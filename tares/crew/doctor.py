"""`tares factory doctor [--json]`: check everything the crew needs and say what to fix.

The port of cmd_doctor, doctor_keys and doctor_kit. The text output is the bash output for every check
bash makes: a header, one `  ok`, `  warn` or `  FIX` line per check, then `all good` or
`fix the items marked FIX`, and exit 1 when any line is FIX. `--json` prints the same checks as one
contract document, {schema_version, ok, checks: [{id, ok, message, fix, level}]} (schemas/doctor.json), for the desktop
app, and exits the same way: 1 when a check is FIX, with the document still on stdout. `ok` is false only for FIX
(a warning is still ok); `level` says which of the three it is; `fix` is what to do, "" for an ok line.

Checks bash does not make are marked `new` and their text starts with a fixed label (NEW_LABELS), so a
test can set them aside when it compares the two engines line by line: the engine in use, the installed
tares against the plugin's min_tares (TR-1064), `tares` and `tares hook` on PATH (TR-1068), the login key
in the keychain or in a file only its owner reads (TR-949), the crew key file's permissions, git, and
Git for Windows on Windows.

The cmux and language-server checks (doctor_cmux, doctor_lsp) are not here: TR-970 ports them, and until
then this doctor prints no line for them.

Several helpers bash shares with other commands are not ported yet (the freeze, the plugin clash, the
trust file, the watch's pid, a station's Claude Code version). They are small readers here, named for the
bash function they stand in for; the ticket that ports the original takes them over.

Standard library, httpx (through the client) and psutil, like the rest of tares.crew.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from . import claude, contract, doctor_lsp, state, version
from .client import TaresClient, _say as client_say, cksum, key_name, settings_missing
from .errors import ClaudeNotFoundError, CrewError, JobStateUnreadableError, SessionListUnreadableError
from .paths import ME, CrewPaths

USAGE = f"usage: {ME} doctor [--json]"
LABELS = {"ok": "ok   ", "warn": "warn ", "fix": "FIX  "}
# What the new checks' text starts with (after the level word), for a test that sets them aside.
NEW_LABELS = ("engine", "tares on PATH", "tares version", "login key", "crew key file", "git", "Git for Windows")
TEMP_FLAGS = re.compile(r"/var/folders/|/tmp/cmux|cmux-claude-settings")
RUN_TIMEOUT = 60
WINDOWS_ALLOWED = ("NT AUTHORITY\\SYSTEM", "BUILTIN\\ADMINISTRATORS", "OWNER RIGHTS", "CREATOR OWNER")


@dataclass
class Check:
    id: str
    level: str          # "ok", "warn" or "fix"
    message: str
    fix: str = ""
    new: bool = False   # a check bash does not make

    @property
    def ok(self) -> bool:
        return self.level != "fix"

    def as_json(self) -> dict:
        return {"id": self.id, "ok": self.ok, "message": self.message, "fix": self.fix, "level": self.level}

    def line(self) -> str:
        return f"  {LABELS[self.level]} {self.message}"


def repo_ids(rows: list) -> list:
    """One id part per (project, repo) row, unique within its project: the repo's folder name, with -2, -3
    after the first when two repos of a project share a folder name or the same repo is listed twice (and
    on to the next free number when that name is a folder name too). The desktop app tells checks apart
    by id, so a project with several repos needs one per repo."""
    used: set = set()
    out = []
    for project, repo in rows:
        base = re.split(r"[\\/]", str(repo).rstrip("/\\"))[-1] or "repo"
        part, n = base, 1
        while (project, part) in used:
            n += 1
            part = f"{base}-{n}"
        used.add((project, part))
        out.append(part)
    return out


def _windows() -> bool:
    """A function so a test can patch it."""
    return sys.platform == "win32"


def slashes(path: str) -> str:
    """A path as JSON and messages show it: forward slashes, on every platform."""
    return path.replace("\\", "/")


def _run(cmd, cwd=None, timeout=RUN_TIMEOUT):
    """A finished command with its output as UTF-8 text, or None when it can't start or takes too long."""
    try:
        return subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


# ---------- readers for helpers bash shares with other commands ----------


def kit_version(root) -> str:
    """bash kit_version: the cksum of every file of the kit read in byte order of its `./relative` path,
    leaving out __pycache__, .pyc, dotfiles, editor leftovers and the like. Never fails."""
    skip_names = {"4913"}
    skip_ends = (".pyc", "~", ".orig", ".rej", ".swp")
    rels = []
    for top, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            full = os.path.join(top, name)
            if name.startswith(".") or name in skip_names or name.endswith(skip_ends) or os.path.islink(full):
                continue
            if os.path.isfile(full):
                rels.append("./" + os.path.relpath(full, root).replace(os.sep, "/"))
    rels.sort(key=lambda r: r.encode("utf-8"))
    data = b""
    for r in rels:
        try:
            data += Path(root, r[2:]).read_bytes()
        except OSError:
            continue
    return str(cksum(data))


def installed_tares(home: str, my_version: str) -> list:
    """bash installed_tares: "name version scope[ (projectPath)]" for each installed tares plugin of
    another version, from Claude Code's installed_plugins.json."""
    doc = state.read_json(Path(home, ".claude", "plugins", "installed_plugins.json"), None)
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    out = []
    if not isinstance(plugins, dict):
        return out
    for k, entries in plugins.items():
        if not k.startswith("tares@") or not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict) or e.get("version") == (my_version or ""):
                continue
            ver = e.get("version")
            line = f"{k} {state.jq_raw(ver)} {state.jq_raw(e.get('scope'))}"
            if e.get("projectPath"):
                line += f" ({state.jq_raw(e['projectPath'])})"
            out.append(line)
    return out


def clash_text(lines: list, kit_plugin: str, my_version: str) -> str:
    """bash clash_text: what to do about another installed version of the plugin."""
    first = lines[0]
    k = first.split(" ", 1)[0]
    joined = ";".join(lines).replace(";", "; ")
    tail = ""
    if " project (" in first:
        where = re.sub(r".*\((.*)\).*", r"\1", first)
        tail = f" --scope project (run it in {where})"
    return (f"another version of the tares plugin is installed from a marketplace ({joined}). Where Claude Code "
            f"loads it, it takes the place of this one ({kit_plugin}, version {my_version}) and stations run "
            f"without the factory skills. If they report missing skills, remove it: claude plugin uninstall {k}{tail}")


def frozen_text(frozen) -> str:
    """bash frozen_text: "since <time> by <who>" from the freeze Tares recorded."""
    at = state.jq_get(frozen, ("at",))
    if at:
        return f"since {at} by {state.jq_get(frozen, ('by',))}"
    return "(Tares does not say since when)"


def cc_retry_in(stations_at: str, now=None) -> str:
    """bash cc_retry_in: how long until the watch tries a station again, "10 min" or "3 h"."""
    at = int(stations_at) if stations_at.isdigit() else 0
    n = max((at - int(time.time() if now is None else now) + 59) // 60, 1)
    return f"{(n + 59) // 60} h" if n >= 120 else f"{n} min"


def watch_running(paths: CrewPaths) -> bool:
    """bash watch_pid: watch.pid names a live process whose command line holds "crew watch" (and, for a Python
    watch, that started when its meta file says)."""
    from . import watch
    return watch.running_pid(paths) is not None


# ---------- Windows: who can read a file ----------


def acl_extras(path, user: str, run=None) -> list | None:
    """The principals on a file's access list besides the current user, SYSTEM and Administrators; [] when
    only they can read it, None when icacls could not be read. Read from `icacls <path>`, whose lines are
    `<path> <who>:(<rights>)` and then `   <who>:(<rights>)`. Denying entries are not grants and are skipped."""
    r = (run or _run)(["icacls", os.fspath(path)])
    if r is None or r.returncode != 0:
        return None
    me = {user.upper(), user.upper().rsplit("\\", 1)[-1]}
    extras = []
    for i, line in enumerate(r.stdout.splitlines()):
        text = line.strip()
        p = os.fspath(path)
        if i == 0 and text.startswith(p):
            text = text[len(p):].strip()
        elif i == 0 and len(text) > len(p) and text[len(p)] == " ":
            # icacls writes the path in the console code page, so a name with accents reads back changed
            text = text[len(p):].strip()
        m = re.match(r"^(.+?):\((.*)$", text)
        if not m or "(DENY" in text.upper() or text.startswith("Successfully"):
            continue
        who = m.group(1).strip()
        if who.upper() in me or who.upper() in WINDOWS_ALLOWED or who.upper().rsplit("\\", 1)[-1] in me:
            continue
        extras.append(who)
    return extras


def owner_only_state(path, run=None) -> tuple:
    """(True, "") when only the owner can read path, (False, who) when others can, (None, why) when it can't be told."""
    p = os.fspath(path)
    if not _windows():
        try:
            mode = stat.S_IMODE(os.stat(p).st_mode)
        except OSError as e:
            return None, e.strerror or str(e)
        return (True, "") if mode & 0o077 == 0 else (False, f"mode {mode:04o}")
    from . import perms
    extras = acl_extras(p, perms.windows_user(), run)
    if extras is None:
        return None, "icacls could not read its access list"
    return (True, "") if not extras else (False, ", ".join(extras))


# ---------- the doctor ----------


class Doctor:
    def __init__(self, env: dict, paths: CrewPaths | None = None, root=None, run=None):
        self.env = env
        self.paths = paths or CrewPaths(env.get("FACTORY_CREW_HOME") or CrewPaths.from_env().raw)
        self._root = root
        self.run = run or _run
        self.checks: list = []
        self.client: TaresClient | None = None
        self.claude_path = ""
        self.sessions = None          # the session list, when it could be read (bash: $list)
        self._trust = None

    # -- plumbing --

    @property
    def root(self) -> str:
        """KIT_PLUGIN: the plugin this CLI ships with."""
        if self._root is None:
            self._root = self.env.get("TARES_PLUGIN_DIR") or str(__import__("tares.cli", fromlist=["x"]).plugin_dir())
        return os.path.realpath(self._root)

    @property
    def home(self) -> str:
        return claude._home(self.env)

    def add(self, level: str, id: str, message: str, fix: str = "", new: bool = False) -> None:
        self.checks.append(Check(id, level, message, fix, new))

    def ok(self, id, msg, new=False):
        self.add("ok", id, msg, "", new)

    def no(self, id, msg, fix, new=False):
        self.add("fix", id, msg, fix, new)

    def warn(self, id, msg, fix, new=False):
        self.add("warn", id, msg, fix, new)

    def trusted_map(self) -> dict:
        if self._trust is None:
            doc = state.read_json(Path(self.home, ".claude.json"), None)
            projects = doc.get("projects") if isinstance(doc, dict) else None
            self._trust = projects if isinstance(projects, dict) else {}
        return self._trust

    def accepted(self, path: str) -> bool:
        row = self.trusted_map().get(path)
        return isinstance(row, dict) and row.get("hasTrustDialogAccepted") is True

    def trusted(self, path: str) -> bool:
        """bash trusted: Claude Code trusts the path, or a linked worktree's main repo."""
        if self.accepted(path):
            return True
        r = self.run(["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"])
        if r is None or r.returncode != 0:
            return False
        main = os.path.dirname(r.stdout.strip())
        return main != path and self.accepted(main)

    # -- the checks --

    def check_basics(self) -> None:
        try:
            self.claude_path = claude.claude_bin(self.env)
        except ClaudeNotFoundError as e:
            self.no("claude", e.message, "install Claude Code, or set CLAUDE_BIN to its path")
        else:
            r = None
            try:
                r = claude.run_claude(["--version"], self.claude_path, claude.VERSION_TIMEOUT)
            except (OSError, subprocess.SubprocessError, CrewError):
                pass
            first = (r.stdout.splitlines() or [""])[0] if r is not None else ""
            self.ok("claude", f"claude: {self.claude_path} ({first})")
        self.ok("crew_home", f"crew home: {self.paths.raw}")
        self.check_engine()
        self.check_tares_version()
        self.check_tares_path()

    def check_engine(self) -> None:
        from . import dispatch
        engine, source = dispatch.engine_setting(self.env)
        if engine == "bash":
            where = "TARES_FACTORY_ENGINE=bash" if source == dispatch.ENGINE_VAR else 'engine: bash in crew.json'
            self.ok("engine", f"engine: bash ({where}; unset it to use the Python engine)", True)
            return
        if engine not in ("", "python"):
            self.no("engine", f'engine: "{engine}" is not an engine', 'use "python" or "bash"', True)
            return
        why = {dispatch.ENGINE_VAR: "TARES_FACTORY_ENGINE=python", "crew.json": "engine: python in crew.json"}.get(
            source, "the default")
        self.ok("engine", f"engine: python ({why}; TARES_FACTORY_ENGINE=bash or engine: bash in crew.json runs bash instead)", True)

    def check_tares_version(self) -> None:
        need = version.plugin_min_version(self.root)
        have = version.installed_version()
        if not need:
            self.ok("min_tares", f"tares version: {have or 'unknown'}, and the plugin names no minimum", True)
            return
        nk, hk = version.parse_version(need), version.parse_version(have)
        if nk is None or hk is None:
            self.warn("min_tares", f"tares version: could not compare the installed {have or 'unknown'} "
                      f"with the {need} the plugin needs", "uv tool upgrade tares, or pip install -U tares", True)
        elif hk < nk:
            self.no("min_tares", f"tares version: the installed tares {have} is older than the {need} the plugin needs",
                    "uv tool upgrade tares, or pip install -U tares", True)
        else:
            self.ok("min_tares", f"tares version: the installed tares {have} meets the {need} the plugin needs", True)

    def check_tares_path(self) -> None:
        """Hooks call `tares`: it must resolve on PATH. `tares hook` is newer; hooks keep to today's commands
        until a release has it (TR-1103), so a tares without it is a warning."""
        found = shutil.which("tares", path=self.env.get("PATH"))
        if not found:
            self.no("tares_path", "tares on PATH: no tares resolves on PATH, and the plugin's hooks call it",
                    "uv tool install tares (or pip install tares), then open a new terminal", True)
            return
        r = self.run([found, "--version"], timeout=30)
        ver = (r.stdout.strip().splitlines() or [""])[0] if r is not None and r.returncode == 0 else ""
        shown = f"{found} ({ver})" if ver else found
        h = self.run([found, "hook", "--help"], timeout=30)
        if h is not None and h.returncode == 0:
            self.ok("tares_path", f"tares on PATH: {shown}, and it has tares hook", True)
        else:
            self.warn("tares_path", f"tares on PATH: {shown} has no tares hook, which the plugin's hooks will call",
                      "uv tool upgrade tares, or pip install -U tares", True)

    def check_tares(self, c: TaresClient) -> bool:
        src = self.env.get("TARES_FACTORY_SOURCE") or "environment"
        if not c.tares_on():
            self.no("tares", f"no Tares answers at {c.tares} (from the {src}): start one with `tares up`, connect to "
                    "your Tares Cloud workspace with `tares login`, or set TARESD_URL to where yours runs",
                    "tares up, or tares login, or set TARESD_URL")
            return False
        self.ok("tares", f"Tares answers at {c.tares} (from the {src})")
        try:
            c.tares_http("GET", "/api/crew/settings")
        except CrewError:
            if src == "saved login":
                self.no("tares_access", f"Tares at {c.tares} refuses the crew: run `tares login` again", "tares login")
            else:
                self.no("tares_access", f"Tares at {c.tares} refuses the crew: run `tares login` again, or set "
                        "TARES_AUTH_TOKEN to an admin key or the root token of your own Tares",
                        "tares login, or set TARES_AUTH_TOKEN")
        else:
            self.ok("tares_access", "Tares lets this crew in")
        self.check_keys(c)
        return True

    def saved_key(self, c: TaresClient) -> str:
        """The saved login's key when it is for this Tares (doctor_keys' `saved`)."""
        data = state.read_json(self.login_path(), None)
        if not isinstance(data, dict):
            return ""
        url = state.jq_raw(data.get("cell_url")) if data.get("cell_url") else ""
        if url.rstrip("/") != c.tares:
            return ""
        return state.jq_raw(data["key"]) if data.get("key") else ""

    def login_path(self) -> Path:
        return Path(self.env.get("TARES_HOME") or Path(self.home, ".tares"), "login.json")

    def check_keys(self, c: TaresClient) -> None:
        saved = self.saved_key(c)
        mine = self.env.get("TARES_AUTH_TOKEN") or ""
        src = self.env.get("TARES_FACTORY_KEY_SOURCE") or ""
        file = self.env.get("TARES_FACTORY_TOKEN_FILE") or str(self.paths.token_file)
        if mine:
            if src == "environment" and saved and mine != saved:
                self.no("terminal_key", f"TARES_AUTH_TOKEN in this terminal ({key_name(mine)}) overrides your saved "
                        f"login ({key_name(saved)}): unset TARES_AUTH_TOKEN, then run this again", "unset TARES_AUTH_TOKEN")
            else:
                self.ok("terminal_key", f"this terminal uses {key_name(mine)}, from the {src or 'environment'}")
        if not self.paths.home.is_dir():
            return
        theirs = c.crew_key()
        if not theirs:
            if not c.key_ok(""):
                self.no("crew_key", f"the stations have no key in {file}: run tares login (for a Tares of your own, "
                        f"run {ME} doctor with TARES_AUTH_TOKEN set)", "tares login")
        elif c.key_ok(theirs):
            self.ok("crew_key", f"stations use {key_name(theirs)} from {file}, and Tares accepts it")
            if saved and theirs != saved:
                self.warn("crew_key_saved", f"the stations' key is not your saved login's ({key_name(saved)})",
                          "tares login")
        else:
            self.no("crew_key", f"stations use {key_name(theirs)} from {file}, and Tares refuses it: run tares login",
                    "tares login")

    def check_key_files(self) -> None:
        """New: the crew's key file, and the login key, are readable by their owner alone (TR-949)."""
        file = self.env.get("TARES_FACTORY_TOKEN_FILE") or str(self.paths.token_file)
        if os.path.isfile(file):
            how = "an access list for you alone" if _windows() else "mode 0600"
            good, why = owner_only_state(file, self.run)
            if good:
                self.ok("crew_key_file", f"crew key file: {file} is readable by you only", new=True)
            elif good is False:
                self.no("crew_key_file", f"crew key file: {file} can be read by others ({why})",
                        f"tares login rewrites it with {how}", True)
            else:
                self.warn("crew_key_file", f"crew key file: could not tell who can read {file} ({why})",
                          f"check it has {how}", True)
        self.check_login_key()

    def check_login_key(self) -> None:
        path = self.login_path()
        data = state.read_json(path, None)
        if not isinstance(data, dict) or not data.get("cell_url"):
            if not self.env.get("TARES_AUTH_TOKEN"):
                self.ok("login_key", "login key: no saved login (a Tares without auth needs none)", new=True)
            return
        if data.get("key_store") == "keychain":
            from .. import login
            key, problem = login._kc_get(login._account(data))
            if key:
                self.ok("login_key", "login key: in the OS keychain", new=True)
                return
            why = {"off": "TARES_KEYRING is not on here, so tares cannot read it",
                   "unusable": "no keychain works here",
                   "error": "the keychain refused (locked, or access denied)"}.get(problem, "the keychain has no entry for it")
            self.no("login_key", f"login key: it is kept in the OS keychain, but {why}",
                    "tares login (or set TARES_KEYRING=on)" if problem == "off" else "tares login", True)
            return
        if not data.get("key"):
            self.warn("login_key", f"login key: {path} holds no key", "tares login", True)
            return
        good, why = owner_only_state(path, self.run)
        if good:
            self.ok("login_key", f"login key: in {path}, readable by you only", new=True)
        elif good is False:
            self.no("login_key", f"login key: {path} holds the key and others can read it ({why})",
                    "tares login (it rewrites the file for you alone)", True)
        else:
            self.warn("login_key", f"login key: could not tell who can read {path} ({why})", "check the file's permissions", True)

    def check_sessions(self) -> None:
        try:
            rows = claude.active_json(self.claude_path or None)
        except (SessionListUnreadableError, ClaudeNotFoundError):
            self.no("sessions", f"{claude.UNREAD}; the watch can't tell which stations run", "check claude agents --json")
            return
        self.sessions = rows
        if all(r.get("kind") == "interactive" or all(k in r for k in ("id", "name", "cwd", "state")) for r in rows):
            self.ok("sessions", f"Claude Code's session list reads ({len(rows)} active)")
        else:
            self.no("sessions", "claude agents --json lists sessions without a name, cwd or state; the watch can't "
                    "tell which stations run", "update Claude Code")

    def check_trust_and_plugin(self) -> None:
        raw = self.paths.raw
        if self.accepted(raw):
            self.ok("home_trusted", f"Claude Code trusts {raw}")
        else:
            self.no("home_trusted", f"Claude Code has not trusted the crew home {raw} (the crew's own folder, whichever "
                    f"folder you run from; its stations run there). {ME} crew up offers to open Claude Code there to "
                    f"trust it, or: mkdir -p {raw} && cd {raw} && claude, accept the trust prompt, /exit",
                    f"{ME} crew up, or: cd {raw} && claude, accept the trust prompt, /exit")
        mine = state.jq_get(state.read_json(Path(self.root, ".claude-plugin", "plugin.json"), None), ("version",))
        clash = installed_tares(self.home, mine)
        if clash:
            first = clash[0].split(" ", 1)[0]
            self.warn("plugin", clash_text(clash, self.root, mine), f"claude plugin uninstall {first}")
        else:
            self.ok("plugin", f"stations load the plugin at {self.root}")

    def check_mcp(self, c: TaresClient) -> None:
        if c.mcp_on():
            self.ok("mcp", f"MCP endpoint answers at {c.mcp_url()}")
        else:
            self.no("mcp", f"no MCP server answers at {c.mcp_url()}; start one with tares mcp --transport "
                    "streamable-http (or set TARES_FACTORY_MCP_URL)", "tares mcp --transport streamable-http")

    def check_crew(self, c: TaresClient) -> None:
        if not self.paths.crew_cfg.is_file():
            self.warn("crew", f"no crew yet in {self.paths.raw}: {ME} crew up creates it", f"{ME} crew up")
            return
        for r in ("orchestrator", "reviewer", "releaser", "builder"):
            m = state.ccfg("stations", r, "model", paths=self.paths)
            if "haiku" in m:
                self.no(f"model_{r}", f"station {r} uses {m}: auto mode doesn't run on Haiku, use sonnet or opus",
                        "set the model to sonnet or opus in crew.json")
        if not c.tares_on():
            return
        try:
            raw = c.settings_raw()
        except CrewError:
            raw = {}
        missing = settings_missing(raw)
        settings = {}
        try:
            settings = c.crew_settings()
        except CrewError:
            pass
        if not missing and settings:
            shown = {k: v for k, v in settings.items() if k != "kit"}
            self.ok("settings", f"crew settings on Tares: {state.dumps_compact(shown)}")
        else:
            self.warn("settings", f"crew settings not answered yet ({' '.join(missing)}); {ME} crew up asks them",
                      f"{ME} crew up")
        self.check_kit(settings)
        if settings.get("idle_wake") is True:
            if watch_running(self.paths):
                self.ok("idle_wake", "idle_wake is on: the watch wakes stations when they have work and holds their ticks")
            else:
                self.warn("idle_wake", "idle_wake is on but the watch is not running: nothing wakes the stations and "
                          f"they hold no tick ({ME} crew watch)", f"{ME} crew watch")
        if not missing and settings.get("releaser") is True and settings.get("release_profile") == "kind":
            for tool, msg, fix in (("docker", "docker not installed (release profile kind)", "install Docker"),
                                   ("kind", "kind not installed (release profile kind): brew install kind", "brew install kind"),
                                   ("kubectl", "kubectl not installed (release profile kind)", "install kubectl")):
                if shutil.which(tool, path=self.env.get("PATH")):
                    self.ok(tool, tool)
                else:
                    self.no(tool, msg, fix)
        if settings.get("claude_update") is False:
            self.warn("claude_update", "crew setting claude_update is off: the watch does not move stations onto a new "
                      "Claude Code (one that restarts for another reason starts on it); "
                      f"{ME} crew update moves them", f"{ME} crew update")
        if not missing and settings.get("challenger") is True:
            if shutil.which("codex", path=self.env.get("PATH")):
                self.ok("codex", "codex (challenger on)")
            else:
                self.no("codex", "challenger is on but the Codex CLI is not installed", "install the Codex CLI")
        self.check_projects(c)

    def check_kit(self, settings: dict) -> None:
        """doctor_kit: the freeze, and whether the checkout has a kit the crew is not following."""
        if not settings:
            return
        frozen = (settings.get("kit") or {}).get("frozen")
        if frozen is None:
            self.ok("kit", "the kit follows the plugin checkout (not frozen)")
            return
        text = frozen_text(frozen)
        copy = self.paths.frozen_kit
        if not copy.is_dir():
            self.no("kit", f"the kit is frozen {text}, but its copy {self.paths.shown('frozen-kit')} is missing, so new "
                    f"starts use the checkout: {ME} crew unfreeze, then {ME} crew freeze",
                    f"{ME} crew unfreeze, then {ME} crew freeze")
        elif kit_version(self.root) != state.jq_get(frozen, ("version",)):
            self.warn("kit", f"the kit is frozen {text}, and the checkout has a newer kit that no station moves onto: "
                      f"{ME} crew unfreeze lets them follow it", f"{ME} crew unfreeze")
        else:
            self.ok("kit", f"the kit is frozen {text}; the checkout has nothing newer")

    def check_projects(self, c: TaresClient) -> None:
        try:
            rows = c.project_repo_rows()
        except CrewError:
            return
        for (p, repo), rid in zip(rows, repo_ids(rows)):
            if not os.path.isdir(repo):
                self.no(f"project_{p}_{rid}", f"project {p}: {repo} does not exist on this machine", "fix the repo path on Tares")
                continue
            if self.trusted(repo):
                self.ok(f"project_{p}_{rid}_trust", f"project {p}: {repo} trusted")
            else:
                self.no(f"project_{p}_{rid}_trust", f"project {p}: Claude Code has not trusted {repo} (its git root must be "
                        "trusted itself); run `claude` there once", f"cd {repo} && claude, accept the trust prompt")
            gh = shutil.which("gh", path=self.env.get("PATH"))
            if not gh:
                continue
            r = self.run([gh, "repo", "view", "--json", "nameWithOwner,viewerPermission", "-q",
                          '.nameWithOwner + " " + .viewerPermission'], cwd=repo)
            line = r.stdout.rstrip("\n") if r is not None and r.returncode == 0 else ""
            name, _, perm = line.rpartition(" ")
            if not _:
                name, perm = line, line
            if perm in ("ADMIN", "MAINTAIN", "WRITE"):
                self.ok(f"project_{p}_{rid}_github", f"project {p}: {repo} is {name} on GitHub ({perm})")
            elif perm == "":
                self.no(f"project_{p}_{rid}_github", f"project {p}: gh can't read the GitHub repo of {repo} (no GitHub remote, "
                        "or no access); builders open PRs there", "gh auth login, and add a GitHub remote")
            else:
                self.no(f"project_{p}_{rid}_github", f"project {p}: gh has {perm} on {name}; builders need write access to "
                        "push and merge", "ask for write access to the repo")

    def check_cmux(self) -> None:
        found = doctor_lsp.cmux_check(self.env)
        if found:
            self.add(found[0], "cmux", found[1])

    def check_lsp(self, c: TaresClient) -> None:
        try:
            rows = c.project_repo_rows()
        except CrewError:
            return
        if not rows:
            return
        plugins = doctor_lsp.read_plugins(self.claude_path)
        for row, rid in zip(rows, repo_ids(rows)):
            for level, project, lang, msg, cmds in doctor_lsp.doctor_rows([row], plugins, self.env):
                self.add(level, f"lsp_{project}_{rid}_{lang.lower()}", msg, " && ".join(cmds))

    def check_git(self) -> None:
        r = self.run(["git", "--version"], timeout=30)
        if r is None or r.returncode != 0:
            self.no("git", "git: not installed, and the crew's worktrees need it",
                    "winget install Git.Git" if _windows() else "brew install git", True)
        else:
            self.ok("git", f"git: {(r.stdout.strip().splitlines() or [''])[0]}", True)
        if _windows():
            found = self.env.get("CLAUDE_CODE_GIT_BASH_PATH") or shutil.which("bash", path=self.env.get("PATH")) or ""
            for d in (self.env.get("ProgramFiles"), self.env.get("ProgramFiles(x86)"), r"C:\Program Files"):
                if not found and d and os.path.isfile(os.path.join(d, "Git", "bin", "bash.exe")):
                    found = os.path.join(d, "Git", "bin", "bash.exe")
            if found and os.path.isfile(found):
                shown = slashes(found)
                self.ok("git_windows", f"Git for Windows: {shown}", True)
            else:
                self.no("git_windows", "Git for Windows: not found, and Claude Code on Windows runs its shell commands "
                        "through Git Bash", "winget install Git.Git", True)

    def check_gh(self) -> None:
        gh = shutil.which("gh", path=self.env.get("PATH"))
        if gh:
            r = self.run([gh, "auth", "status"])
            if r is not None and r.returncode == 0:
                self.ok("gh", "gh signed in")
            else:
                self.no("gh", "gh not signed in: gh auth login", "gh auth login")
        else:
            self.no("gh", "gh not installed (needed for PRs, reviews and merges): brew install gh", "brew install gh")

    def check_stations(self) -> None:
        if not self.paths.stations.is_file():
            return
        try:
            stations = state.stations_load(self.paths)
        except CrewError:
            return
        active = [(s, row) for s, row in stations.items() if isinstance(row, dict) and row.get("status") == "active"]
        if self.sessions is not None:
            target = claude.claude_version(self.claude_path or None)
            for s, _row in active:
                if (self.paths.home / "stations" / f"{s}.done").is_file():
                    continue
                v = claude.running_version(state.stations_get(s, "id", paths=self.paths), self.sessions,
                                           self.claude_path or None)
                older = claude.older_than(v, target)
                if older and state.stations_get(s, "cc_failed", paths=self.paths) == target:
                    when = cc_retry_in(state.stations_get(s, "cc_retry_at", paths=self.paths))
                    self.warn(f"cc_{s}", f"{s} runs Claude Code {v}, older than the {target} installed, and keeps coming "
                              f"back on an old spare; the watch retries in {when}, or {ME} crew update now",
                              f"{ME} crew update now")
                elif older:
                    self.warn(f"cc_{s}", f"{s} runs Claude Code {v}, older than the {target} installed; {ME} crew update "
                              "moves it when it's idle", f"{ME} crew update")
                elif v:
                    self.ok(f"cc_{s}", f"{s} runs Claude Code {v}")
                else:
                    self.warn(f"cc_{s}", f"could not tell which Claude Code {s} runs", "claude agents --json")
        for _s, row in active:
            job = row.get("id")
            if not job:
                continue
            try:
                flags = claude.job_state(str(job)).get("respawnFlags") or []
            except JobStateUnreadableError:
                continue
            if isinstance(flags, list) and any(isinstance(f, str) and TEMP_FLAGS.search(f) for f in flags):
                self.no(f"job_{job}", f"station job {job} has temp paths in its saved launch flags; it will fail to "
                        f"respawn. {ME} crew down + {ME} crew up", f"{ME} crew down, then {ME} crew up")

    # -- the whole run --

    def collect(self) -> list:
        # bash runs this before a local-only command such as doctor: the template's defaults under crew.json,
        # then the person's key copied for the stations (and its warning, on stderr, when Tares refuses it)
        state.merge_crew_defaults(self.paths)
        self.check_basics()
        with TaresClient(self.paths, self.env, say=lambda _m: None) as c:
            self.client = c
            c.tares_on()
            quiet, c.say = c.say, client_say
            try:
                c.keep_token()
            finally:
                c.say = quiet
            on = self.check_tares(c)
            self.check_key_files()
            self.check_sessions()
            self.check_trust_and_plugin()
            self.check_mcp(c)
            self.check_crew(c) if on else self.check_crew_offline()
            self.check_cmux()
            if on:
                self.check_lsp(c)
        self.check_git()
        self.check_gh()
        self.check_stations()
        return self.checks

    def check_crew_offline(self) -> None:
        """Tares does not answer: bash still reads crew.json (models, or no crew yet)."""
        if not self.paths.crew_cfg.is_file():
            self.warn("crew", f"no crew yet in {self.paths.raw}: {ME} crew up creates it", f"{ME} crew up")
            return
        for r in ("orchestrator", "reviewer", "releaser", "builder"):
            m = state.ccfg("stations", r, "model", paths=self.paths)
            if "haiku" in m:
                self.no(f"model_{r}", f"station {r} uses {m}: auto mode doesn't run on Haiku, use sonnet or opus",
                        "set the model to sonnet or opus in crew.json")


def cmd_doctor(argv: list, env: dict) -> int:
    """`doctor [--json]`. argv starts with the word doctor."""
    args = argv[1:]
    if any(a != "--json" for a in args):
        if contract.wants_json(args):
            contract.fail("usage", USAGE, contract.EXIT_USAGE)
        print(USAGE, file=sys.stderr)
        return 2
    try:
        checks = Doctor(env).collect()
    except CrewError as e:
        if not contract.refuse(e, args):
            print(e.line, file=sys.stderr)
        return 1
    bad = any(not c.ok for c in checks)
    if args:
        contract.emit({"ok": not bad, "checks": [c.as_json() for c in checks]}, "doctor")
        return 1 if bad else 0
    print(f"{ME} doctor")
    for c in checks:
        print(c.line())
    print("fix the items marked FIX" if bad else "all good")
    return 1 if bad else 0
