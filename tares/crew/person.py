"""The commands a person runs on the crew from their own terminal: `projects`, `finish`, `context`, `plan`,
`spec` and `session`. The port of cmd_projects, cmd_finish, cmd_context, cmd_plan, read_quiet, send_meter,
local_only, cmd_spec, cmd_session and the helpers the last two call (person_claude, spec_contributor,
session_dir, session_project, session_idea, match_project, factory_projects, releasing_projects, repos_in,
has_commit).

  tares factory projects [--json]            every project handed to the crew: progress, milestone, builders, repos
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

from . import contract, state
from .claude import _child_env, _creationflags, _home, claude_bin, claude_command, job_state
from .client import TaresClient, TaresHTTPError
from .errors import CrewError
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
        flags = ["--plugin-dir", kit_dir(client, env)]
        # the station's own settings and plugin, so the tools and skills counted are the ones it loads
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
        return 0


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


def person_claude(client: TaresClient, env: dict, cwd: str, sname: str, sfile: str, prompt: str,
                  extra: dict | None = None) -> int:
    """The person's own Claude Code session, in the foreground in `cwd`, with this plugin turned on (its own
    --settings, as for stations), told the crew's Tares and MCP endpoint, and the key as a file. Returns
    Claude's exit code. `extra` is more variables for Claude's environment."""
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
    claude_bin(env)  # bash looks for Claude Code before anything else
    client = prelude(["spec", *args], env)
    with client:
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
                raise CrewError(f"{root} has no commit yet. Make a first commit and push it, so builders can branch from it.")
            here = root
            client.say(f"planning in the repo {root}")
        else:
            repos = repos_in(here)
            if not repos:
                raise CrewError(f"run {ME} spec inside the git repo the project builds on, or in a folder that holds its repos")
            client.say(f"planning in {here}, which holds {len(repos)} repo(s): "
                       + ", ".join(os.path.basename(r) for r in repos))
        slug = _cut_slug(name or _words(idea, 4))
        if not slug:
            slug = state.slugify(os.path.basename(here))
        if contrib:
            _err(f"{ME}: opening Claude as {slug}-spec in {here}, as a contributor to {cproject}, which {clead} leads.\n"
                 "         It asks you the questions for you and shows the tickets in your repos.\n")
            return person_claude(client, env, here, f"{slug}-spec", "spec",
                                 f"You are contributing to {cproject} led by {clead}. Load the spec-project skill, "
                                 "section Contributor.", {"TARES_PROJECT": cproject, "TARES_SESSION_KIND": "contributor"})
        _err(f"{ME}: opening Claude as {slug}-spec in {here}, with /tares:spec.\n"
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
    _err(f"{ME}: opening Claude as {slug}-session in {root or here}.\n"
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
    claude_bin(env)  # bash looks for Claude Code before anything else
    client = prelude(["session", *args], env)
    with client:
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
