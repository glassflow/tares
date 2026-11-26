"""The commands a person runs on the crew from their own terminal: `projects`, `setup`, `finish`, `context`, `plan`,
`spec` and `session`. The port of cmd_projects, cmd_finish, cmd_context, cmd_plan, read_quiet, send_meter,
local_only, cmd_spec, cmd_session and the helpers the last two call (person_claude, spec_contributor,
session_dir, session_project, session_idea, match_project, factory_projects, releasing_projects, repos_in,
has_commit).

  tares factory projects [--json]            every project handed to the crew: progress, milestone, builders, repos
  tares factory setup --json [--progress | --repos [--credential <name>] [--via app]]   the cell's factory setup (JSON only)
  tares factory finish <project> ["words"]   the person marks a project done (never a station)
  tares factory context <role|station>       read a station's /context and send it to Tares
  tares factory plan                         read the plan's limits (/usage) and send them to Tares
  tares factory spec [--project <name>] ["<idea>"]    open Claude with /tares:spec in the repo or folder of repos
  tares factory session [--new] [<project>|"<idea>"]  open Claude bound to a project, or planning an idea

`context` and `plan` need Claude Code itself, so they run here and send the reading to Tares through its
claude_code source. Neither makes a model call; hooks and the status line are off for them.

Where bash and this port differ, bash is right. The one thing bash does not have is `projects --json`, a contract document (schemas/projects.json).
Standard library plus the Tares client (httpx). Nothing here imports the daemon or DuckDB.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from urllib.parse import quote

from . import contract, state
from .claude import _child_env, _creationflags, _home, claude_bin, claude_command, job_state
from .client import TaresClient, TaresHTTPError
from .errors import CrewError, UsageError
from .paths import ME, CrewPaths, default_home

READ_TIMEOUT = 600  # seconds a reading may take; bash waits as long as Claude Code does

# ---------- which commands work while Tares is down ----------


def local_only(cmd: str, sub: str = "") -> bool:
    """Commands that only touch the sessions on this machine work while Tares is down: stopping the crew
    must never wait for Tares. `sub` is the word after the command (`crew halt`)."""
    if cmd in ("tidy", "_parse-bg-id", "_mcp-url", "help", "doctor", "halt", "resume", "down", "attach",
               "logs", "retire", "roster"):
        return True
    if cmd == "crew":
        return (sub or "status") in ("halt", "resume", "down", "attach", "logs")
    return False


def _paths(env: dict) -> CrewPaths:
    return CrewPaths(env.get("FACTORY_CREW_HOME") or default_home(env))


def prelude(argv: list, env: dict) -> TaresClient:
    """What bash does before every command: refuse a crew name that is not valid, then either note where
    Tares is (a local-only command) or decide once where Tares is and what the settings are (every other)."""
    paths = _paths(env)
    state.crew_name(paths)
    client = TaresClient(paths, env)
    if local_only(argv[0] if argv else "", argv[1] if len(argv) > 1 else ""):
        state.merge_crew_defaults(paths)
        client.tares_on()
        client.keep_token()
    else:
        client.prime()
    return client


# ---------- small helpers ----------


def _t(value) -> str:
    """What jq prints for a value inside "\\(...)": null as null, a whole number without a point."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value if isinstance(value, str) else state.dumps_compact(value)


def _truthy(value) -> bool:
    """jq's `select(.x)` and `//`: anything but null and false."""
    return value is not None and value is not False


def _usage(text: str) -> int:
    print(f"usage: {ME} {text}", file=sys.stderr)
    return 1


# ---------- the kit a reading runs on ----------


def frozen_phys(paths: CrewPaths) -> str:
    """The frozen copy as stations record it: physical, so a symlinked crew home still matches."""
    home = os.path.realpath(paths.raw) if os.path.isdir(paths.raw) else paths.raw
    return f"{home}/frozen-kit"


def _plugin_dir(env: dict) -> str:
    """The checkout or install the kit comes from (TARES_PLUGIN_DIR is the test hook)."""
    override = env.get("TARES_PLUGIN_DIR")
    if override:
        return override
    from ..cli import plugin_dir  # imported when used: a command that reads no session does not need it
    return str(plugin_dir())


def kit_dir(client: TaresClient, env: dict) -> str:
    """The kit a station starts on: the frozen copy while the crew is frozen, else this checkout. Tares
    unreadable: a copy on disk means frozen, so a blip never moves a station."""
    paths = client.paths
    if paths.frozen_kit.is_dir():
        try:
            frozen = (client.crew_settings().get("kit") or {}).get("frozen") is not None
        except CrewError:
            frozen = True
        if frozen:
            return frozen_phys(paths)
    plugin = _plugin_dir(env)
    # A factory run from the frozen copy still means the checkout it was copied from
    if paths.checkout_file.is_file():
        here = frozen_phys(paths) + "/"
        if (plugin.rstrip("/") + "/").startswith(here):
            try:
                c = state.read_text(paths.checkout_file).rstrip("\n")
            except OSError:
                c = ""
            if c and os.path.isdir(c):
                return c
    return plugin


# ---------- the readings ----------


def read_quiet(cwd: str, settings: str, args: list) -> list:
    """Run `claude -p` in cwd with no saved session, the settings file (or JSON text) given, and the
    stream-json output; the lines it printed. Nothing printed when it cannot start or fails."""
    cmd = claude_command(claude_bin(), ["-p", "--no-session-persistence", "--settings", settings,
                                        "--output-format", "stream-json", "--verbose", *args])
    try:
        out = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                             env=_child_env(), timeout=READ_TIMEOUT, creationflags=_creationflags()).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return (out or "").splitlines()


def last_field(lines: list, key: str):
    """The value of `key` in the last line that is a JSON object having it (not null or false); None if none."""
    found = None
    for line in lines:
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and _truthy(doc.get(key)):
            found = doc[key]
    return found


def send_meter(client: TaresClient, doc: dict) -> None:
    """One reading to Tares's claude_code source. TaresHTTPError when it is not taken."""
    client.tares_http("POST", "/ingest/claude_code", doc)


def station_name(role: str, paths: CrewPaths) -> str:
    if role in ("orchestrator", "reviewer", "releaser"):
        return f"{state.crew_name(paths)}-{role}"
    return role


def cmd_context(argv: list, env: dict) -> int:
    try:
        return _context(argv[1:], env)
    except CrewError as e:
        print(e.line, file=sys.stderr)
        return 1


def _context(args: list, env: dict) -> int:
    client = prelude(["context", *args], env)
    with client:
        paths = client.paths
        state.require_crew(paths)
        if not args or not args[0]:
            return _usage("context <role|station>")
        s = station_name(args[0], paths)
        job = state.stations_get(s, "id", paths=paths)
        if not job:
            raise CrewError(f"unknown station {s}")
        try:
            sid = _t_str(job_state(job).get("sessionId"))
        except CrewError:
            sid = ""
        sid = sid or state.stations_get(s, "sessionId", paths=paths)
        if not sid:
            raise CrewError(f"{s} has no conversation to read yet")
        cwd = state.stations_get(s, "cwd", paths=paths) or paths.raw
        settings = state.stations_get(s, "settings", paths=paths)
        flags = _launch_flags(client, s, paths, env)
        # the station's own settings, sources and plugin, so the tools and skills counted are the ones it loads
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            state.atomic_write_text(tmp, state.dumps_pretty(_reading_settings(settings)) + "\n")
            # a running session is read through a copy (--fork-session), which is never saved
            lines = read_quiet(cwd, tmp, [*flags, "--resume", sid, "--fork-session", "/context"])
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        usage = last_field(lines, "context_usage")
        if usage is None:
            raise CrewError(f"Claude Code gave no /context reading for {s} (session {sid})")
        try:
            send_meter(client, {"type": "session_context", "sessionId": sid, "timestamp": state.utc_stamp(),
                                "context_usage": usage})
        except TaresHTTPError:
            raise CrewError(f"could not send the reading of {s} to Tares") from None
        u = usage if isinstance(usage, dict) else {}
        print(f"{s}: {_t(u.get('total_tokens'))} of {_t(u.get('raw_max_tokens'))} tokens "
              f"({_t(u.get('percentage'))}%), sent to Tares")
        print(launch_summary(u))
        return 0


# what a station's launch passes that decides what loads: the setting sources, the plugins, the MCP servers
# and the browser extension. Other launch flags (model, prompt, permission mode) do not change what is counted.
_LOAD_VALUED = ("--setting-sources", "--plugin-dir", "--mcp-config")
_LOAD_BARE = ("--no-chrome", "--strict-mcp-config")


def _load_flags(args: list) -> list:
    """The flags of a launch's `claude` arguments that decide what loads, in order, with their values."""
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in _LOAD_BARE:
            out.append(a)
        elif a in _LOAD_VALUED and i + 1 < len(args):
            out += [a, args[i + 1]]
            i += 1
        i += 1
    return out


def _launch_flags(client: TaresClient, station: str, paths: CrewPaths, env: dict) -> list:
    """The load flags the station's own launch passes (launch_plan, so a change to the launch follows here).
    Without a plan (the station's role is not recorded, or Tares will not give its launch and no copy stands
    in): the lean flags and the kit a launch starts with."""
    from . import launch
    flags = []
    row = {k: state.stations_get(station, k, paths=paths) for k in ("role", "parent", "project", "repo")}
    if row["role"]:
        try:
            plan = launch.launch_plan(client, station, row["role"], row["parent"], row["project"], row["repo"])
            flags = _load_flags(plan["args"])
        except CrewError:
            pass
    if "--plugin-dir" not in flags:
        flags = ["--plugin-dir", kit_dir(client, env), *flags]
    if "--setting-sources" not in flags:
        flags += list(launch.LEAN_FLAGS)
    return flags


def launch_summary(usage: dict) -> str:
    """One line on what a reading shows loaded: the tokens before any message, the tools per server, the skills
    and whether auto-memory is on. Counts and server names only; no tool, path or setting value."""
    def num(x):
        return x if isinstance(x, int) and not isinstance(x, bool) else 0
    cats = [c for c in usage.get("categories") or [] if isinstance(c, dict)]
    messages = sum(num(c.get("tokens")) for c in cats if c.get("name") == "Messages")
    tools: dict = {}
    for t in usage.get("mcp_tools") or []:
        if isinstance(t, dict):
            name = str(t.get("server_name") or "?")
            tools[name] = tools.get(name, 0) + 1
    skills = [k for k in usage.get("skills") or [] if isinstance(k, dict)]
    memory = any(isinstance(m, dict) and str(m.get("type") or "").startswith("AutoMem")
                 for m in usage.get("memory_files") or [])
    per = ", ".join(f"{n} {tools[n]}" for n in sorted(tools)) or "none"
    return (f"launch: {max(num(usage.get('total_tokens')) - messages, 0)} tokens before messages; "
            f"tools by server: {per}; skills: {len(skills)}; memory: {'on' if memory else 'off'}")


def _t_str(value) -> str:
    """`jq -r '.x // empty'`."""
    return "" if not _truthy(value) else (value if isinstance(value, str) else state.dumps_compact(value))


def _reading_settings(path: str) -> dict:
    """The station's settings with hooks off and no status line; hooks off alone when there are none to read."""
    if path and os.path.isfile(path):
        try:
            doc = state.loads_json(state.read_text(path))
        except (OSError, ValueError):
            doc = None
        if isinstance(doc, dict):
            doc = dict(doc)
            doc["disableAllHooks"] = True
            doc.pop("statusLine", None)
            return doc
    return {"disableAllHooks": True}


def cmd_plan(argv: list, env: dict) -> int:
    try:
        return _plan(env)
    except CrewError as e:
        print(e.line, file=sys.stderr)
        return 1


def _plan(env: dict) -> int:
    client = prelude(["plan"], env)
    with client:
        from . import proc   # here: proc imports the launch
        if proc.crew_on_codex(client.paths):
            return _codex_plan(client)
        home = client.paths.raw
        cwd = home if os.path.isdir(home) else os.getcwd()
        if sys.platform != "win32":
            lines = read_quiet(cwd, '{"disableAllHooks": true}', ["/usage"])
        else:
            # cmd.exe refuses JSON with quotes in a claude.cmd command line: pass the same settings as a file
            fd, tmp = tempfile.mkstemp(suffix=".json")
            os.close(fd)
            try:
                state.atomic_write_text(tmp, state.dumps_pretty(_reading_settings("")) + "\n")
                lines = read_quiet(cwd, tmp, ["/usage"])
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        report = last_field(lines, "usage_report")
        if report is None:
            raise CrewError("Claude Code gave no /usage report (it reads one for a Pro or Max login only)")
        try:
            send_meter(client, {"type": "claude_plan", "timestamp": state.utc_stamp(), "usage_report": report})
        except TaresHTTPError:
            raise CrewError("could not send the plan's limits to Tares") from None
        limits = (report.get("rate_limits") or {}).get("limits") if isinstance(report, dict) else None
        for lim in limits if isinstance(limits, list) else []:
            if isinstance(lim, dict):
                print(f"{_t(lim.get('kind'))}: {_t(lim.get('percent'))}% used, resets {_t(lim.get('resets_at'))}")
        return 0


def _codex_plan(client: TaresClient) -> int:
    """`plan` for a crew on Codex (TR-1597): the Codex account's limits, read from one of its stations and
    sent to Tares. No `claude` is asked."""
    from . import codex_pace
    reading = codex_pace.take(client.paths)
    if reading is None:
        raise CrewError("no station of this crew answers, and a Codex account's limits are read from a running "
                        "station; start the crew, then run this again")
    try:
        client.tares_http("PUT", codex_pace.LIMITS_PATH, {k: reading[k] for k in ("limits", "account", "taken_at")})
    except TaresHTTPError:
        raise CrewError("could not send the Codex account's limits to Tares") from None
    shown = 0
    for bucket, snap in codex_pace.buckets(reading["limits"]):
        for w in snap:
            mins = w.get("windowDurationMins")
            length = f"{_t(mins)} minutes" if mins is not None else "a window of unknown length"
            resets = state.utc_stamp(w["resetsAt"]) if isinstance(w.get("resetsAt"), (int, float)) else "unknown"
            print(f"{bucket + ', ' if bucket else ''}{length}: {_t(w.get('usedPercent'))}% used, resets {resets}")
            shown += 1
    if not shown:
        kind = _t(reading["account"].get("type")) or "this"
        print(f"Codex reports no limits for this account ({kind}): there is nothing to pace by")
    return 0


# ---------- finish ----------


def cmd_finish(argv: list, env: dict) -> int:
    try:
        return _finish(argv[1:], env)
    except CrewError as e:
        print(e.line, file=sys.stderr)
        return 1


def _finish(args: list, env: dict) -> int:
    client = prelude(["finish", *args], env)
    with client:
        if env.get("FACTORY_STATION"):
            raise CrewError("only the person runs this. As a station, ask the person on the desk and call "
                            "finish_project with their answer.")
        if not args or not args[0]:
            return _usage('finish <project> ["your words"]')
        name, words = args[0], " ".join(args[1:])
        row = client.project_row(name)
        if row is None:
            names = ", ".join(_t_raw(p.get("name")) for p in client.crew_projects())
            raise CrewError(f"{name} is not a project the crew holds (open projects: {names})")
        pid = _t_raw(row.get("id"))
        headers = {"Content-Type": "application/json", **client._auth(client.tares_token())}
        try:
            r = client._request("POST", f"{client.tares}/api/projects/{pid}/finish", headers,
                                state.dumps_compact({"words": words}).encode("utf-8"), 15)
        except TaresHTTPError:
            raise CrewError(f"could not reach Tares at {client.tares}") from None
        body = r.text.rstrip("\n")
        if r.status_code != 200:
            raise CrewError(f"Tares did not finish {name}: {_detail(body)}")
        print(f"✓ {_t_raw(row.get('name'))} is done. It is off the crew's work list; {client.tares}/projects/{pid} shows it.")
        try:
            doc = state.loads_json(body)
        except ValueError:
            doc = None
        left = doc.get("open_projects") if isinstance(doc, dict) else None
        if isinstance(left, (int, float)) and not isinstance(left, bool) and left == 0:
            print(f"  The crew holds no other project. Stop it with: {ME} crew down")
        return 0


def _t_raw(value) -> str:
    """`jq -r .x`: a string as is, null as null."""
    return value if isinstance(value, str) else _t(value)


def _detail(body: str) -> str:
    """`jq -r '.detail // .'` of the body, or the body itself when it is not JSON."""
    if not body.strip():
        return ""
    try:
        doc = state.loads_json(body)
    except ValueError:
        return body
    if isinstance(doc, dict) and _truthy(doc.get("detail")):
        return state.jq_raw(doc["detail"])
    return state.jq_raw(doc)


# ---------- projects ----------


def builders_of(name, paths: CrewPaths) -> int:
    """Active builder stations of the project, from stations.json; 0 when it can't be read."""
    try:
        stations = state.stations_load(paths)
    except (CrewError, OSError, ValueError):
        return 0
    return sum(1 for v in stations.values()
               if isinstance(v, dict) and v.get("role") == "builder" and v.get("project") == name
               and v.get("status") == "active")


def _milestone(p: dict) -> str:
    m = p.get("milestone")
    if _truthy(m):
        m = m if isinstance(m, dict) else {}
        return f"{_t_raw(m.get('name'))} {_t(m.get('done'))}/{_t(m.get('total'))}"
    total = p.get("tickets_total")
    if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
        return "all done"
    return "-"


def _repos(p: dict) -> str:
    out = []
    for r in p.get("repos") or []:
        v = r.get("name") if isinstance(r, dict) else None
        if not _truthy(v):
            v = r.get("path") if isinstance(r, dict) else None
        out.append("" if v is None else _t_raw(v))
    return ", ".join(out)


def cmd_projects(argv: list, env: dict) -> int:
    try:
        return _projects(argv[1:], env)
    except CrewError as e:
        if not contract.refuse(e, argv):
            print(e.line, file=sys.stderr)
        return 1


def _projects(args: list, env: dict) -> int:
    client = prelude(["projects", *args], env)
    with client:
        if "--json" in args:
            # a script must tell "no projects" from "Tares refused the list"; the text view keeps bash's answer
            client.crew_projects_or_die()
        rows = [p for p in client.crew_projects() if isinstance(p, dict)]
        if "--json" in args:
            out = [{**p, "builders": builders_of(p.get("name"), client.paths)} for p in rows]
            contract.emit({"projects": out}, "projects")
            return 0
        if not rows:
            print(f"no projects handed to the crew yet: {ME} spec in a repo starts one")
            return 0
        print(f"{'PROJECT':<28} {'DONE':<8} {'MILESTONE':<22} {'BUILDERS':<9} REPOS")
        for p in rows:
            name = _t_raw(p.get("name"))
            done = f"{_t(p.get('tickets_done'))}/{_t(p.get('tickets_total'))}"
            print(f"{name[:28]:<28} {done:<8} {_milestone(p)[:22]:<22} {builders_of(p.get('name'), client.paths):<9} {_repos(p)}")
        return 0


# ---------- setup ----------
#
# `setup --json` hands a program the cell's factory setup (GET /api/factory/setup and its two neighbours), so a
# desktop app can draw the screen the console draws. The cell owns the shape; this passes it on in a document
# (schemas/setup.json). A new command with no bash twin.

SETUP_USAGE = (f"usage: {ME} setup --json | {ME} setup --progress --json | "
               f"{ME} setup --repos [--credential <name>] [--via app] --json")


class SetupError(CrewError):
    """A refusal of the setup routes with its own error code (`forbidden`, `cell_too_old`, `key_refused`)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _setup_args(args: list) -> tuple:
    """(kind, credential, via) from the arguments after the word `setup`: kind is "setup", "progress" or
    "repos"; credential and via are None when not given. A bad combination is a UsageError."""
    kind, credential, via, i = "setup", None, None, 0
    seen = set()

    def value(flag: str, inline):
        nonlocal i
        if inline is None:
            i += 1
            inline = args[i] if i < len(args) else ""
        if not inline or inline.startswith("--"):
            raise UsageError(f"{flag} needs a value; {SETUP_USAGE}")
        return inline

    while i < len(args):
        a = args[i]
        flag, eq, inline = a.partition("=")
        if a == "--json":
            pass
        elif a in ("--progress", "--repos"):
            seen.add(a)
        elif flag in ("--credential", "--via"):
            got = value(flag, inline if eq else None)
            if flag == "--credential":
                credential = got
            else:
                via = got
        else:
            raise UsageError(f"unknown argument {a}; {SETUP_USAGE}")
        i += 1
    if len(seen) > 1:
        raise UsageError(f"--progress and --repos cannot be combined; {SETUP_USAGE}")
    if seen:
        kind = seen.pop()[2:]
    if kind != "repos" and (credential is not None or via is not None):
        raise UsageError(f"--credential and --via go with --repos; {SETUP_USAGE}")
    return kind, credential, via


def cmd_setup(argv: list, env: dict) -> int:
    """`setup --json`, `setup --progress --json`, `setup --repos [--credential <name>] [--via app] --json`.
    argv starts with the word setup. JSON only: without --json it is a usage line on stderr, exit 2."""
    if not contract.wants_json(argv):
        print(f"{ME}: setup prints JSON only; {SETUP_USAGE}", file=sys.stderr)
        return contract.EXIT_USAGE
    try:
        return _setup(argv[1:], env)
    except CrewError as e:
        contract.refuse(e, argv)
        return contract.EXIT_USAGE if isinstance(e, UsageError) else contract.EXIT_FAILED


def _setup_get(client: TaresClient, path: str):
    """The JSON object Tares answers at path. Its refusals get the codes of schemas/README.md: 401 `key_refused`,
    403 `forbidden`, 404 `cell_too_old` (the route is newer than the cell), anything else `tares_error`."""
    try:
        body = client.tares_http("GET", path)
    except TaresHTTPError as e:
        why = f": {e.detail}" if e.detail else ""
        if e.status == 401:
            raise SetupError("key_refused", f"Tares at {client.tares} refused the key{why}{client.no_key_hint()}") from None
        if e.status == 403:
            raise SetupError("forbidden", f"Tares would not give the setup to this key{why or ' (HTTP 403)'}") from None
        if e.status == 404:
            raise SetupError("cell_too_old", f"Tares at {client.tares} is too old to give the factory setup. "
                             "Upgrade it, then run this again.") from None
        if e.status is None:
            raise SetupError("tares_error", f"could not reach Tares at {client.tares}") from None
        raise SetupError("tares_error", f"Tares did not give the setup (HTTP {e.status}){why}") from None
    try:
        doc = state.loads_json(body)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise SetupError("tares_error", f"Tares at {client.tares} answered {path} with something that is not a JSON object")
    return doc


def _setup(args: list, env: dict) -> int:
    kind, credential, via = _setup_args(args)
    client = prelude(["setup", *args], env)
    with client:
        if kind == "progress":
            contract.emit({"progress": _setup_get(client, "/api/factory/setup/progress")}, "setup")
        elif kind == "repos":
            query = "&".join(f"{k}={quote(v, safe='')}" for k, v in (("credential", credential), ("via", via)) if v)
            doc = _setup_get(client, "/api/factory/setup/github/repos" + (f"?{query}" if query else ""))
            repos = doc.get("repos")
            if not isinstance(repos, list):
                raise SetupError("tares_error", f"Tares at {client.tares} did not give a list of repositories")
            if doc.get("error"):
                # the route says `error` only when nothing was listed; `note` is the sentence the console shows
                raise SetupError("tares_error", f"GitHub did not list the repositories: {str(doc['error']).rstrip('.')}.")
            contract.emit({"repos": repos}, "setup")
        else:
            contract.emit({"setup": _setup_get(client, "/api/factory/setup")}, "setup")
    return 0


# ---------- spec and session: the person's own Claude Code session ----------
#
# `spec` and `session` open Claude Code in the foreground, in the folder the person is working in, with this
# plugin turned on and told where Tares and its MCP endpoint are. Nothing is written into the repos. They are
# the person's alone: a station never runs them.


def _read_line():
    """One line from stdin without its newline, as `IFS= read -r` gives it. None at the end of input, also
    for a last line with no newline: bash's `read` fails there, and every caller treats that as no answer."""
    line = sys.stdin.readline()
    return line[:-1] if line.endswith("\n") else None


def _err(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()


def choose(question: str, default: int, options: list) -> str:
    """One question with numbered choices; Enter takes the recommended one. Takes the number or the value.
    options are "value|label" strings. The prompts go to stderr, as bash prints them."""
    vals = [o.split("|", 1)[0] for o in options]
    _err(f"\n{question}\n")
    for i, o in enumerate(options, 1):
        label = o.split("|", 1)[1] if "|" in o else o
        _err(f"  {i}) {label}{'   (recommended)' if i == default else ''}\n")
    while True:
        _err(f"  Choice [{default}]: ")
        ans = _read_line()
        if ans is None:
            ans = ""
            _err("\n")
        ans = ans or str(default)
        if ans.isascii() and ans.isdigit() and 1 <= int(ans) <= len(vals):
            return vals[int(ans) - 1]
        if ans in vals:
            return ans
        _err(f"  Pick a number from 1 to {len(vals)}.\n")


def _git(args: list, cwd=None):
    """(exit code, stdout without its last newline) of a git command; (1, "") when git cannot run."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    except OSError:
        return 1, ""
    return r.returncode, r.stdout.rstrip("\n")


# ---------- a new project's repo ----------
# `spec` plans in a git repo that has a commit on GitHub: builders branch from it and the crew opens pull requests
# there. A person starting from an empty folder is offered to make that repo here (asked only at a terminal); anyone
# else is told the commands.


def _repo_name(folder: str) -> str:
    """A GitHub repo name from a folder's name: letters, digits, '.', '-' and '_'."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(os.path.normpath(folder))).strip("-.") or "project"


def _has_origin(path: str) -> bool:
    return _git(["-C", path, "remote", "get-url", "origin"])[0] == 0


def first_repo_commands(folder: str, is_repo: bool) -> list:
    """The commands that make `folder` a GitHub repo with a first commit, as a person would type them."""
    name = _repo_name(folder)
    out = [] if is_repo else ["git init"]
    out.append('git commit --allow-empty -m "Start"')
    out.append("git push -u origin HEAD" if is_repo and _has_origin(folder)
               else f"gh repo create {name} --private --source . --remote origin --push")
    return out


def _say_commands(cmds: list) -> str:
    return "".join(f"\n           {c}" for c in cmds)


def _ask_yes(question: str) -> bool:
    """A yes or no at a terminal; False when there is none to ask (a script, the crew), or on anything but yes."""
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
    except (AttributeError, ValueError):
        return False
    _err(f"{ME}: {question} [y/N] ")
    try:
        line = sys.stdin.readline()
    except (OSError, ValueError):
        return False
    return line.strip().lower() in ("y", "yes")


def _run_step(cmd: list, cwd: str) -> None:
    """Run one step of making the repo; a failure stops with the tool's own last line."""
    try:
        r = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", errors="replace")
    except OSError:
        raise CrewError(f"could not run {cmd[0]}: install it, or make the repo yourself") from None
    if r.returncode != 0:
        last = ((r.stderr or r.stdout).strip().splitlines() or [f"{cmd[0]} failed"])[-1]
        raise CrewError(f"{' '.join(cmd[:3])} failed: {last}")


def make_first_repo(folder: str, is_repo: bool) -> None:
    """Make `folder` a private GitHub repo with an empty first commit, pushed: what `first_repo_commands` says."""
    name = _repo_name(folder)
    if not is_repo:
        _run_step(["git", "init"], folder)
    _run_step(["git", "commit", "--allow-empty", "-m", "Start"], folder)
    if is_repo and _has_origin(folder):
        _run_step(["git", "push", "-u", "origin", "HEAD"], folder)
    else:
        _run_step(["gh", "repo", "create", name, "--private", "--source", ".", "--remote", "origin", "--push"], folder)
    _err(f"{ME}: {folder} is now the GitHub repo {name}, with a first commit.\n"
         "         Make sure the Tares app on GitHub can reach it (all repositories, or this one picked).\n")


def has_commit(path: str) -> bool:
    return _git(["-C", path, "rev-parse", "-q", "--verify", "HEAD"])[0] == 0


def git_root() -> str:
    """The git repo the current folder is in, or ""."""
    rc, out = _git(["rev-parse", "--show-toplevel"])
    # git prints C:/dir on Windows; the rest of the program uses the system's own form
    return os.path.normpath(out) if rc == 0 and out else ""


def repos_in(folder: str) -> list:
    """Each git repo directly beneath the folder that has a commit: the folders (links to folders too) that
    are the top of their own repo, in name order."""
    found = []
    try:
        names = sorted(n for n in os.listdir(folder) if not n.startswith("."))
    except OSError:
        return found
    for n in names:
        d = os.path.join(folder, n)
        if not os.path.isdir(d):
            continue
        rc, top = _git(["-C", d, "rev-parse", "--show-toplevel"])
        if rc == 0 and os.path.normcase(os.path.realpath(top)) == os.path.normcase(os.path.realpath(d)) and has_commit(d):
            found.append(d)
    return found


def match_project(key: str, rows: list):
    """The first row whose name or id is `key`, or whose name has the same slug as `key`; None if none."""
    slug = state.slugify(key)
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = r.get("name")
        if name == key or r.get("id") == key or (isinstance(name, str) and state.slugify(name) == slug):
            return r
    return None


def _get_json(client: TaresClient, path: str):
    """The JSON object Tares answers a GET with, or None when it cannot say."""
    try:
        doc = state.loads_json(client.tares_http("GET", path))
    except (CrewError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def factory_projects(client: TaresClient) -> list:
    """The software factory projects on Tares as {id, name, status}; none when Tares cannot say."""
    doc = _get_json(client, "/api/projects")
    rows = doc.get("projects") if doc else None
    if not isinstance(rows, list):
        return []
    return [{"id": p.get("id"), "name": p.get("name"), "status": p.get("status") if _truthy(p.get("status")) else "active"}
            for p in rows if isinstance(p, dict) and p.get("kind") == "software_factory"]


def releasing_projects(client: TaresClient) -> list:
    """The projects a person took over from the crew that are still being released; none when Tares cannot say."""
    doc = _get_json(client, "/api/crew/projects")
    rel = doc.get("releasing") if doc else None
    return rel if isinstance(rel, list) else []


def _tsv(value) -> str:
    """One field of jq's @tsv: null empty, tab, newline, carriage return and backslash escaped."""
    text = "" if value is None else _t(value)
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def spec_contributor(client: TaresClient, key: str):
    """(project name, lead) when the caller is a contributor to the software factory project `key` names (its
    name, id or slug): someone who is not its lead. Anything else, or any failure of the lookup, is None, so
    the normal spec flow runs. Both are as jq's @tsv writes them."""
    if not key:
        return None
    doc = _get_json(client, "/api/projects")
    if doc is None:
        return None
    rows = []
    for p in doc.get("projects") or []:
        if not isinstance(p, dict) or p.get("kind") != "software_factory":
            continue
        lead = p.get("lead")
        if lead is not None and not isinstance(lead, dict):
            return None
        lead = lead or {}
        rows.append({"id": p.get("id"), "name": p.get("name"),
                     "lead_id": lead.get("id") if _truthy(lead.get("id")) else "",
                     "lead_name": lead.get("name") if _truthy(lead.get("name")) else ""})
    row = match_project(key, rows)
    if row is None or row["lead_id"] == "":
        return None
    who = _get_json(client, "/api/whoami")
    person = who.get("person") if who else None
    me = state.jq_get(person, ("id",)) if isinstance(person, dict) else ""
    if not me or me == row["lead_id"]:
        return None
    lead_name = row["lead_name"] if row["lead_name"] != "" else row["lead_id"]
    return _tsv(row["name"]), _tsv(lead_name)


def _kit_plugin(env: dict) -> str:
    """The plugin folder this script ships in, physical: bash's KIT_PLUGIN."""
    return os.path.realpath(_plugin_dir(env))


def plugin_version(kit: str) -> str:
    return state.jq_get(state.read_json(os.path.join(kit, ".claude-plugin", "plugin.json"), None), ("version",))


def installed_tares(home: str, my_version: str) -> list:
    """"name version scope[ (projectPath)]" for each installed tares plugin of another version, from Claude
    Code's installed_plugins.json."""
    doc = state.read_json(os.path.join(home, ".claude", "plugins", "installed_plugins.json"), None)
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
            line = f"{k} {state.jq_raw(e.get('version'))} {state.jq_raw(e.get('scope'))}"
            if e.get("projectPath"):
                line += f" ({state.jq_raw(e['projectPath'])})"
            out.append(line)
    return out


def clash_text(lines: list, kit_plugin: str, my_version: str) -> str:
    """What to do about another installed version of the plugin."""
    first = lines[0]
    k = first.split(" ", 1)[0]
    tail = ""
    if " project (" in first:
        where = re.sub(r".*[(](.*)[)].*", r"\1", first)
        tail = f" --scope project (run it in {where})"
    return (f"another version of the tares plugin is installed from a marketplace ({'; '.join(lines)}). Where Claude "
            f"Code loads it, it takes the place of this one ({kit_plugin}, version {my_version}) and stations run "
            f"without the factory skills. If they report missing skills, remove it: claude plugin uninstall {k}{tail}")


def _warn_clash(client: TaresClient, env: dict) -> None:
    kit = _kit_plugin(env)
    version = plugin_version(kit)
    lines = installed_tares(_home(env), version)
    if lines:
        client.say(f"warning: {clash_text(lines, kit, version)}")


def on_codex(client: TaresClient, env: dict | None = None) -> bool:
    """Does this crew run on Codex? Its recorded stations say, and Tares is not asked. A crew that has recorded
    none is on Claude Code where Claude Code is installed, as bash had it (nothing more is asked of Tares), and
    where neither Claude Code nor Codex is (the person is told Claude Code is missing before Tares is asked
    anything). On a machine with Codex and no Claude Code, the cell's runtime slot says (TR-1599)."""
    from . import codex as cx
    from . import launch, proc
    env = os.environ if env is None else env
    have = launch.crew_runtime(client.paths)
    if have:
        return have == proc.CODEX
    try:
        claude_bin(env)
        return False
    except CrewError:
        pass
    try:
        cx.codex_bin(env)
    except CrewError:
        return False   # no Codex either: the missing Claude Code is said, as before
    try:
        return proc.planned_runtime(client) == proc.CODEX
    except Exception:  # noqa: BLE001 - a client that cannot ask Tares: Claude Code, as before
        return False


def app_name(client: TaresClient, env: dict | None = None) -> str:
    return "Codex" if on_codex(client, env) else "Claude"


def codex_prompt(prompt: str) -> str:
    """A planning session's first prompt as a Codex session takes it: Claude Code's slash command becomes words
    (a Codex session has its manual in its instructions, and no slash commands of the plugin)."""
    if prompt.startswith("/tares:spec"):
        rest = prompt[len("/tares:spec"):].strip()
        return "Plan a project with me, as your spec manual says." + (f" {rest}" if rest else "")
    if prompt.startswith("/tares:session"):
        return prompt[len("/tares:session"):].strip()
    return prompt.replace("Load the spec-project skill, section Contributor.",
                          "Follow the page `contributor.md` of your spec manual.")


def _toml_str(value: str) -> str:
    """A TOML basic string (JSON's escapes are TOML's)."""
    return json.dumps(value, ensure_ascii=False)


def person_codex(client: TaresClient, env: dict, cwd: str, sname: str, sfile: str, prompt: str,
                 extra: dict | None = None) -> int:
    """The person's own Codex session for planning (TR-1599), the `spec` or `session` manual as its developer
    instructions and the crew's Tares MCP endpoint with the crew's key (through the plugin's headers helper, as a
    station's), all given on the command line: the person's own ~/.codex is neither read for them nor changed.
    In a terminal it is Codex's own interactive session, in the foreground in `cwd`; with no terminal
    (`TARES_PLANNING_EXEC` set, or no tty) it is one `codex exec` run that takes the first prompt as the whole
    brief. Returns Codex's exit code. `extra` is more variables for its environment."""
    from . import codex as cx
    from . import role_text
    from .launch import shell_word
    paths = client.paths
    role = "spec" if sfile == "spec" else "session"
    kit = kit_dir(client, env)
    try:
        manual = role_text.manual(kit, role_text.CODEX, role)
    except role_text.RenderError as e:
        raise CrewError(f"cannot give the {role} session its rules on Codex: {e}") from None
    if not manual:
        raise CrewError(f"the kit at {kit} has no {role} manual for Codex")
    child = {k: v for k, v in env.items() if k not in ("TARES_AUTH_TOKEN", "TARES_FACTORY_SOURCE", "TARES_FACTORY_KEY_SOURCE")}
    if env.get("TARES_AUTH_TOKEN") and not env.get("FACTORY_STATION"):
        paths.home.mkdir(parents=True, exist_ok=True)
        client.keep_token()
    tares = client.tares or client.tares_url()
    child.update(TARES_FACTORY_URL=tares, TARESD_URL=tares, TARES_FACTORY_MCP_URL=client.mcp_url())
    if not child.get("FACTORY_CREW"):
        try:
            child["FACTORY_CREW"] = state.crew_name(paths)
        except CrewError:
            pass
    child.update(extra or {})
    if not os.path.isdir(cwd):
        raise CrewError(f"cannot open Codex in {cwd}: no such folder")
    helper = (f"TARES_FACTORY_CRED_FILE={shell_word(str(paths.token_file))} tares hook mcp-headers "
              f"--plugin-root {shell_word(kit)}")
    overrides = {
        "developer_instructions": _toml_str(manual),
        "mcp_servers.tares.url": _toml_str(client.mcp_url()),
        "mcp_servers.tares.required": "true",
        "mcp_servers.tares.default_tools_approval_mode": _toml_str("approve"),
        "mcp_servers.tares.http_headers_helper": _toml_str(helper),
        "mcp_servers.tares.env_http_headers": '{"X-Tares-Crew" = "FACTORY_CREW", "X-Tares-Project" = "TARES_PROJECT"}',
    }
    flags = [a for k, v in overrides.items() for a in ("-c", f"{k}={v}")]
    codex = cx.codex_bin(env)
    first = codex_prompt(prompt)
    interactive = not env.get("TARES_PLANNING_EXEC") and sys.stdin.isatty() and sys.stdout.isatty()
    if interactive:
        cmd = [codex, *flags, first]
    else:
        first = (first + "\n\nNobody answers questions in this run: take this prompt as the whole brief, decide what "
                 "it leaves open yourself and record each such decision as an assumption, and finish without waiting.")
        cmd = [codex, "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", *flags, first]
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    old = None
    try:
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    try:
        code = subprocess.run(cmd, cwd=cwd, env=child).returncode
    except FileNotFoundError:
        print(f"{ME}: {codex}: command not found", file=sys.stderr)
        return 127
    finally:
        if old is not None:
            try:
                signal.signal(signal.SIGINT, old)
            except (ValueError, OSError):
                pass
    return 128 - code if code < 0 else code


def person_claude(client: TaresClient, env: dict, cwd: str, sname: str, sfile: str, prompt: str,
                  extra: dict | None = None) -> int:
    """The person's own Claude Code session, in the foreground in `cwd`, with this plugin turned on (its own
    --settings, as for stations), told the crew's Tares and MCP endpoint, and the key as a file. Returns
    Claude's exit code. `extra` is more variables for Claude's environment. On a crew that runs on Codex it
    is the person's own Codex session instead (person_codex)."""
    if on_codex(client, env):
        return person_codex(client, env, cwd, sname, sfile, prompt, extra)
    paths = client.paths
    args = ["-n", sname, "--plugin-dir", kit_dir(client, env)]
    child = {k: v for k, v in env.items() if k not in ("TARES_AUTH_TOKEN", "TARES_FACTORY_SOURCE", "TARES_FACTORY_KEY_SOURCE")}
    if env.get("TARES_AUTH_TOKEN") and not env.get("FACTORY_STATION"):
        paths.home.mkdir(parents=True, exist_ok=True)
        client.keep_token()
        # CRED_FILE: Claude Code hides variables named *TOKEN* from the plugin's header helper
        tok = paths.shown(paths.token_file.name)
        child["TARES_FACTORY_TOKEN_FILE"] = tok
        child["TARES_FACTORY_CRED_FILE"] = tok
    paths.stations_dir.mkdir(parents=True, exist_ok=True)
    state.write_json(paths.station_file(sfile, "settings.json"), {"enabledPlugins": {"tares@inline": True}})
    args += ["--settings", paths.shown(f"stations/{sfile}.settings.json")]
    tares = client.tares or client.tares_url()
    child.update(TARES_FACTORY_URL=tares, TARESD_URL=tares, TARES_FACTORY_MCP_URL=client.mcp_url())
    # the plugin stamps X-Tares-Crew from FACTORY_CREW: without it a person's hand_over on a named crew
    # was recorded under `crew` and named crew-orchestrator, which is not theirs
    if not child.get("FACTORY_CREW"):
        try:
            child["FACTORY_CREW"] = state.crew_name(paths)
        except CrewError:
            pass  # a crew name that cannot be one: the call names no crew, as before
    child.update(extra or {})
    if not os.path.isdir(cwd):
        raise CrewError(f"cannot open Claude in {cwd}: no such folder")
    if sys.platform != "win32":
        child["PWD"] = os.path.abspath(cwd)
    claude = claude_bin(env)
    cmd = claude_command(claude, [*args, prompt])
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass
    # Ctrl-C belongs to Claude, which is in the foreground; this process only waits for it
    old = None
    try:
        old = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    try:
        code = subprocess.run(cmd, cwd=cwd, env=child).returncode
    except FileNotFoundError:
        print(f"{ME}: {claude}: command not found", file=sys.stderr)
        return 127
    except OSError as e:
        print(f"{ME}: could not start {claude}: {e.strerror or e}", file=sys.stderr)
        return 126
    finally:
        if old is not None:
            try:
                signal.signal(signal.SIGINT, old)
            except (ValueError, OSError):
                pass
    return 128 - code if code < 0 else code


def _words(text: str, n: int) -> str:
    """The first n whitespace-separated words, joined by one space."""
    return " ".join([w for w in re.split(r"[ \t\n\r\f\v]+", text) if w][:n])


def _cut_slug(text: str) -> str:
    """slugify, cut to 40 characters, one trailing hyphen dropped."""
    slug = state.slugify(text)[:40]
    return slug[:-1] if slug.endswith("-") else slug


# ---------- spec ----------


def cmd_spec(argv: list, env: dict) -> int:
    try:
        return _spec(argv[1:], env)
    except CrewError as e:
        print(e.line, file=sys.stderr)
        return 1


def _spec(args: list, env: dict) -> int:
    client = prelude(["spec", *args], env)
    with client:
        if not on_codex(client, env):
            claude_bin(env)  # a crew on Claude Code looks for it first, as bash did
        idea = name = ""
        args = list(args)
        while args:
            a = args[0]
            if a == "--project":
                if len(args) < 2 or args[1] == "":
                    return _usage('spec [--project <name>] ["<idea>"]')
                name = args[1]
                del args[:2]
            elif a == "--":
                idea = " ".join(args[1:])
                break
            elif a.startswith("-"):
                raise CrewError(f"unknown option {a}")
            else:
                idea = f"{idea} {a}" if idea else a
                del args[:1]
        client.require_mcp()
        _warn_clash(client, env)
        contrib = spec_contributor(client, name or idea)
        cproject = clead = ""
        here = os.path.realpath(os.getcwd())
        root = git_root()
        if contrib:
            cproject, clead = contrib
            if root:
                here = root
        elif root:
            if not has_commit(root):
                if not _ask_yes(f"{root} has no commit yet. Make a first commit and push it to GitHub now?"):
                    raise CrewError(f"{root} has no commit yet. Make a first commit and push it, so builders can "
                                    "branch from it:" + _say_commands(first_repo_commands(root, True)))
                make_first_repo(root, True)
            here = root
            client.say(f"planning in the repo {root}")
        else:
            repos = repos_in(here)
            if not repos:
                if not _ask_yes(f"{here} is not a git repo. Make it a private GitHub repo with a first commit, "
                                "and plan the project in it?"):
                    raise CrewError(f"run {ME} spec inside the git repo the project builds on, or in a folder that "
                                    "holds its repos. A new project? Make its repo here first:"
                                    + _say_commands(first_repo_commands(here, False)))
                make_first_repo(here, False)
                here = git_root() or here
                client.say(f"planning in the repo {here}")
            else:
                client.say(f"planning in {here}, which holds {len(repos)} repo(s): "
                           + ", ".join(os.path.basename(r) for r in repos))
        slug = _cut_slug(name or _words(idea, 4))
        if not slug:
            slug = state.slugify(os.path.basename(here))
        if contrib:
            _err(f"{ME}: opening {app_name(client, env)} as {slug}-spec in {here}, as a contributor to {cproject}, which {clead} leads.\n"
                 "         It asks you the questions for you and shows the tickets in your repos.\n")
            return person_claude(client, env, here, f"{slug}-spec", "spec",
                                 f"You are contributing to {cproject} led by {clead}. Load the spec-project skill, "
                                 "section Contributor.", {"TARES_PROJECT": cproject, "TARES_SESSION_KIND": "contributor"})
        _err(f"{ME}: opening {app_name(client, env)} as {slug}-spec in {here}"
             + (", with its spec manual.\n" if on_codex(client, env) else ", with /tares:spec.\n") +
             "         It interviews you, one question at a time, and writes the spec, milestones and tickets\n"
             f"         into a project on Tares ({client.tares}). It ends by asking whether to hand the project to the crew.\n")
        prompt = "/tares:spec"
        if name:
            prompt += f" (project name: {name})"
        if idea:
            prompt += f" {idea}"
        return person_claude(client, env, here, f"{slug}-spec", "spec", prompt)


# ---------- session ----------


def session_dir(client: TaresClient, project_id: str) -> str:
    """The folder a project's session opens in: the repo you are in when it is one of the project's, else its
    first repo that is on this machine, else where you are."""
    here = os.path.realpath(os.getcwd())
    root = git_root()
    doc = _get_json(client, f"/api/projects/{project_id}/crew")
    handover = doc.get("handover") if doc else None
    repos = handover.get("repos") if isinstance(handover, dict) else None
    paths = []
    for r in repos if isinstance(repos, list) else []:
        if isinstance(r, dict) and _truthy(r.get("path")):
            paths.append(state.jq_raw(r["path"]))
    if root and root in paths:
        return root
    for p in paths:
        if p and os.path.isdir(p):
            return p
    return root or here


def session_project(client: TaresClient, env: dict, row: dict) -> int:
    pid, name = _t_raw(row.get("id")), _t_raw(row.get("name"))
    crew = client.project_row(name)
    rel = match_project(name, releasing_projects(client))
    taken = ""
    if crew is not None:
        ms = crew.get("milestone")
        on = f", on {_t_raw(ms.get('name') if isinstance(ms, dict) else None)}" if _truthy(ms) else ""
        print(f"{name} is with the crew: {_t(crew.get('tickets_done'))} of {_t(crew.get('tickets_total'))} "
              f"tickets done{on}.", file=sys.stderr)
        take = choose("Take it over? The crew stops working on it: its builders push what they have and write their "
                      "progress, then this session carries on from there.", 2,
                      ["yes|yes, take it over and build it in this session", "no|no, leave it with the crew"])
        if take != "yes":
            print(f"Left with the crew. Talk to it with: {ME} crew attach orchestrator", file=sys.stderr)
            return 0
        _err("\nIn a few words, why (kept on Tares with the takeover; Enter to skip)\n> ")
        words = _read_line() or ""
        headers = {"Content-Type": "application/json", **client._auth(client.tares_token())}
        try:
            r = client._request("POST", f"{client.tares}/api/projects/{pid}/takeover", headers,
                                state.dumps_compact({"words": words}).encode("utf-8"), 15)
        except TaresHTTPError:
            raise CrewError(f"could not reach Tares at {client.tares}") from None
        if r.status_code != 200:
            raise CrewError(f"Tares did not record the takeover of {name}: {_detail(r.text.rstrip(chr(10)))}")
        taken = "1"
        print(f"Taken over. The orchestrator stops the builders of {name}; the session waits for it, then carries on.",
              file=sys.stderr)
    elif rel is not None:
        taken = "1"
        print(f"{name} was taken over from the crew; the session checks its builders have stopped, then carries on.",
              file=sys.stderr)
    slug = _cut_slug(name)
    prompt = f"/tares:session {name}" + (" (taken over from the crew)" if taken else "")
    return person_claude(client, env, session_dir(client, pid), f"{slug}-session", "session", prompt,
                         {"TARES_PROJECT": pid, "TARES_PROJECT_NAME": name, "TARES_SESSION_TAKEOVER": taken,
                          "TARES_SESSION_NAME": f"{slug}-session"})


def session_idea(client: TaresClient, env: dict, idea: str) -> int:
    here = os.path.realpath(os.getcwd())
    root = git_root()
    slug = _cut_slug(_words(idea, 4)) or "new"
    _err(f"{ME}: opening {app_name(client, env)} as {slug}-session in {root or here}.\n"
         f"         It plans \"{idea}\" lightly into a new project on Tares ({client.tares}) and builds it in the same\n"
         "         session; if it turns out big, it offers the full spec and the hand-over to the crew.\n")
    return person_claude(client, env, root or here, f"{slug}-session", "session", f"/tares:session new: {idea}",
                         {"TARES_SESSION_NAME": f"{slug}-session"})


def cmd_session(argv: list, env: dict) -> int:
    try:
        return _session(argv[1:], env)
    except CrewError as e:
        print(e.line, file=sys.stderr)
        return 1


def _session(args: list, env: dict) -> int:
    client = prelude(["session", *args], env)
    with client:
        if not on_codex(client, env):
            claude_bin(env)  # a crew on Claude Code looks for it first, as bash did
        if env.get("FACTORY_STATION"):
            raise CrewError("only the person runs this, in their own terminal")
        target, new = "", False
        args = list(args)
        while args:
            a = args[0]
            if a == "--new":
                new = True
                del args[:1]
            elif a == "--":
                target = " ".join(([target] if target else []) + args[1:])
                break
            elif a.startswith("-"):
                raise CrewError(f"unknown option {a}")
            else:
                target = f"{target} {a}" if target else a
                del args[:1]
        client.require_mcp()
        _warn_clash(client, env)
        # a project the crew holds must never look free: stop when Tares can't say which it holds
        client.crew_projects_or_die()
        try:
            client.tares_http("GET", "/api/projects")
        except TaresHTTPError as e:
            raise CrewError(f"Tares at {client.tares} did not list its projects "
                            f"({(str(e).splitlines() or [''])[-1]})") from None
        everything = factory_projects(client)
        if new:
            if not target:
                raise CrewError(f'usage: {ME} session --new "<what you want to build>"')
            return session_idea(client, env, target)
        if target:
            row = match_project(target, everything)
            if row is not None:
                return session_project(client, env, row)
            if " " not in target:
                yn = choose(f'No project named {target} on Tares. Plan and build "{target}" as a new project?', 2,
                            ["yes|yes, a new project", "no|no, I meant an existing one"])
                if yn != "yes":
                    names = ", ".join(_t_raw(r["name"]) for r in everything if r["status"] != "done")
                    raise CrewError(f"the open projects: {names}")
            return session_idea(client, env, target)
        # no argument: the open projects, or something new
        crew, rel = client.crew_projects(), releasing_projects(client)
        options = []
        for p in everything:
            if p["status"] == "done":
                continue
            nm = _t_raw(p["name"])
            mark = ""
            if match_project(nm, crew) is not None:
                mark = " (with the crew)"
            if match_project(nm, rel) is not None:
                mark = " (taken over, being released)"
            options.append(f"{_t_raw(p['id'])}|{nm}{mark}")
        options.append("+new|something new")
        pick = choose("Which project?", 1, options)
        if pick == "+new":
            idea = ""
            while not idea:
                _err("\nWhat do you want to build?\n> ")
                line = _read_line()
                if line is None:
                    raise CrewError("nothing to build")
                idea = line
            return session_idea(client, env, idea)
        row = match_project(pick, everything)
        if row is None:
            raise CrewError(f"{pick} is not a project on Tares")
        return session_project(client, env, row)
