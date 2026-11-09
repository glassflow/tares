"""The commands a person runs on the crew from their own terminal: `projects`, `finish`, `context` and
`plan`. The port of cmd_projects, cmd_finish, cmd_context, cmd_plan, read_quiet, send_meter and local_only.

  tares factory projects [--json]            every project handed to the crew: progress, milestone, builders, repos
  tares factory finish <project> ["words"]   the person marks a project done (never a station)
  tares factory context <role|station>       read a station's /context and send it to Tares
  tares factory plan                         read the plan's limits (/usage) and send them to Tares

`context` and `plan` need Claude Code itself, so they run here and send the reading to Tares through its
claude_code source. Neither makes a model call; hooks and the status line are off for them.

Where bash and this port differ, bash is right. The one thing bash does not have is `projects --json`.
Standard library plus the Tares client (httpx). Nothing here imports the daemon or DuckDB.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

from . import state
from .claude import _child_env, _creationflags, claude_bin, claude_command, job_state
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
            print(state.dumps_pretty(out))
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
