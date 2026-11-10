"""Starting and stopping stations from Python: `tares factory spawn` (TR-954).

Ports bash's cmd_spawn (claude-plugin/bin/factory) and station_name. The other lifecycle commands (revive,
restart, retire, down, halt) come in TR-955; they reuse what is here.

Standard library only, plus the crew modules (no duckdb, fastapi or daemon).
"""
from __future__ import annotations

import os
import subprocess
import sys

from . import claude, launch, locks, pace, state
from .client import TaresClient
from .errors import CrewError
from .paths import ME, CrewPaths

SPAWN_USAGE = 'usage: factory spawn builder [--autonomy L3-review] <project> <slug> <repo path> "<task>"'
HELPER_USAGE = 'usage: factory spawn helper <parent-station> <slug> "<task>"'


def station_name(word: str, paths: CrewPaths | None = None) -> str:
    """bash's station_name: orchestrator, reviewer and releaser are this crew's stations; anything else is
    already a station name."""
    if word in ("orchestrator", "reviewer", "releaser"):
        return f"{state.crew_name(paths)}-{word}"
    return word


def _git(*args: str) -> str:
    """git's output without its trailing newline, or "" when git fails (bash's `$(git ... 2>/dev/null)`)."""
    try:
        p = subprocess.run(["git", *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return ""
    if p.returncode != 0:
        return ""
    return p.stdout.decode("utf-8", "replace").rstrip("\r\n")


def _real(path: str) -> str:
    return os.path.realpath(path)


def _repo_label(repo: dict) -> str:
    """jq's `.path // .name`: the path unless it is null or false."""
    p = repo.get("path")
    return state.jq_raw(p if p is not None and p is not False else repo.get("name"))


def _builder_plan(client: TaresClient, paths: CrewPaths, args: list) -> dict:
    """Everything a builder spawn decides before it takes the lock: its place in the crew and its repo."""
    autonomy = ""
    if args[:1] == ["--autonomy"]:
        autonomy = args[1] if len(args) > 1 else ""
        if len(args) >= 2:
            args = args[2:]
    if len(args) < 4:
        raise CrewError(SPAWN_USAGE)
    reading = pace.read(client)
    if reading.paced:
        raise CrewError(f"no new builder starts now. {state.jq_raw(reading.raw.get('note'))} Spawn it after that, or when the plan has "
                        "room. Helpers are not affected.")
    if autonomy:
        # A project may be stricter than the crew, never looser: a looser level is a new grant from the person.
        want = launch.level_rank(autonomy)
        if want is None:
            raise CrewError("autonomy must be L3-review or L4-ship")
        have = launch.level_rank(client.setting("autonomy"))
        if have is None or want > have:
            raise CrewError(f"a project can only be stricter than the crew (crew autonomy is {client.setting('autonomy')}); "
                            "the person raises the crew setting instead")
    client.crew_projects_or_die()
    row = client.project_row(args[0])
    if row is None:
        raise CrewError(f"{args[0]} is not handed to the crew on Tares; the spec session hands it over (hand_over), "
                        "or run /tares:ship in its repo")
    project = state.jq_raw(row.get("name"))
    slug = state.slugify(args[1])
    task = args[3]
    if not slug or not state.slugify(project):
        raise CrewError("the project name and the slug need letters or digits a station name can use (a-z, 0-9)")
    if not autonomy:
        autonomy = state.jq_get(row, ("autonomy",))
    cn = state.crew_name(paths)
    pslug = state.slugify(project)
    if pslug == cn or pslug.startswith(f"{cn}-"):
        raise CrewError(f'a project name can\'t start with "{cn}": station names starting {cn}- belong to the crew itself')
    repo = _git("-C", args[2], "rev-parse", "--show-toplevel")
    if not repo:
        raise CrewError(f"{args[2]} is not a git repository")
    real = _real(repo)
    repos = row.get("repos") or []
    ok = any(isinstance(r, dict) and r.get("path") and os.path.isdir(r["path"]) and _real(r["path"]) == real for r in repos)
    if not ok:
        raise CrewError(f"{repo} is not one of {project}'s repos on Tares ({', '.join(_repo_label(r) for r in repos)}); "
                        "a repo joins the project at its hand-over")
    station = state.builder_name(pslug, slug, paths)
    cfg_max = client.setting("builders_max")
    stations = state.stations_load(paths)
    active = sum(1 for v in stations.values() if isinstance(v, dict) and v.get("role") == "builder" and v.get("status") == "active")
    try:
        limit = int(cfg_max)
    except ValueError:
        limit = -1  # bash's `[ "$active" -lt "$max" ]` fails on a non-number, and so the builder is refused
    if active >= limit:
        raise CrewError(f"already {active} active builders (crew setting builders_max is {cfg_max}); retire one first")
    os.makedirs(os.path.join(repo, ".factory", "worktrees"), exist_ok=True)
    common = _git("-C", repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:
        exclude = os.path.join(common, "info", "exclude")
        try:
            with open(exclude, encoding="utf-8") as f:
                present = ".factory/" in f.read().split("\n")
        except OSError:
            present = False
        if not present:
            os.makedirs(os.path.dirname(exclude), exist_ok=True)
            with open(exclude, "a", encoding="utf-8", newline="\n") as f:
                f.write(".factory/\n")
    return {
        "role": "builder", "station": station, "parent": state.crew_orchestrator(paths), "project": project,
        "project_id": state.jq_raw(row.get("id")), "repo": repo, "cwd": repo, "autonomy": autonomy, "task": task,
        "model": state.ccfg("stations", "builder", "model", paths=paths),
        "effort": state.ccfg("stations", "builder", "effort", paths=paths),
    }


def _helper_plan(paths: CrewPaths, kind: str, args: list) -> dict:
    if kind == "releaser-helper":
        parent = station_name("releaser", paths)
    else:
        parent = station_name(args[0] if args else "", paths)
        args = args[1:]
    if len(args) < 2:
        raise CrewError(HELPER_USAGE)
    if not state.stations_get(parent, "role", paths=paths):
        raise CrewError(f"{parent} is not a recorded station")
    get = lambda key: state.stations_get(parent, key, paths=paths)  # noqa: E731
    return {
        "role": "helper", "station": f"{parent}-{state.slugify(args[0])}", "parent": parent, "project": get("project"),
        "repo": get("repo"), "cwd": get("cwd"), "model": get("model"), "effort": get("effort"), "task": args[1],
    }


def helper_prompt(paths: CrewPaths, parent: str, task: str) -> str:
    return (f"Boot as a helper of the station {parent}: load the skills factory-protocol and "
            f"station-{launch.parent_role(parent, paths)}, do only the task below, write results to your report, "
            f"and send {parent} DONE when it is finished.\n\nYour task:\n{task}")


def spawn(client: TaresClient, paths: CrewPaths, args: list, *, claude_bin=None) -> None:
    """factory spawn builder|helper|releaser-helper ...: start a station under its name."""
    state.require_crew(paths)
    client.require_mcp()
    if state.halted(paths):
        # bash prints the whole HALT file here (`$(cat HALT)`), not only its reason
        try:
            held = paths.halt_file.read_text(encoding="utf-8", errors="replace").rstrip("\r\n")
        except OSError:
            held = ""
        raise CrewError(f"the crew is halted ({held}); {ME} crew resume first")
    kind, rest = (args[0] if args else ""), list(args[1:])
    if kind == "builder":
        plan = _builder_plan(client, paths, rest)
    elif kind in ("helper", "releaser-helper"):
        plan = _helper_plan(paths, kind, rest)
    else:
        raise CrewError("spawn takes builder or helper")
    station, cwd = plan["station"], plan["cwd"]
    locks.station_lock(station, paths)
    if claude.is_alive(station, cwd, claude_bin, paths):
        raise CrewError(f"{station} is already running; message it instead")
    if plan["role"] == "builder":
        state.stations_put(station, {"autonomy": plan["autonomy"], "project_id": plan["project_id"]}, paths)
        try:
            paths.station_file(station, "done").unlink()
        except FileNotFoundError:
            pass
        prompt = launch.boot_prompt(client, "builder", plan["task"])
    else:
        prompt = helper_prompt(paths, plan["parent"], plan["task"])
    launch.launch(client, station, plan["role"], plan["parent"], plan["project"], plan["repo"], cwd,
                  plan["model"], plan["effort"], prompt, claude=claude_bin)


def cmd_spawn(argv: list, env: dict) -> int:
    """`tares factory spawn ...`; a refusal prints its line to stderr and exits 1, as bash's die does."""
    paths = CrewPaths.from_env()
    try:
        state.crew_name(paths)
        client = TaresClient(paths, env)
        try:
            client.prime()
            spawn(client, paths, list(argv[1:]))
        finally:
            client.close()
    except CrewError as e:
        sys.stdout.flush()
        print(e.line, file=sys.stderr)
        return 1
    return 0


SETTINGS_USAGE = "usage: settings <station> [role]"


def settings(client: TaresClient, paths: CrewPaths, argv: list) -> str:
    """bash's cmd_settings: write the settings a station launches with and return the text of the file.
    The role is the second word, else the station's role in stations.json, else the station's name without
    the crew's prefix. A missing station word is refused with the usage line."""
    state.require_crew(paths)
    if not argv or not argv[0]:
        raise CrewError(SETTINGS_USAGE)
    station = station_name(argv[0], paths)
    role = argv[1] if len(argv) > 1 and argv[1] else state.stations_get(station, "role", paths=paths)
    if not role:
        prefix = state.crew_name(paths) + "-"
        role = station[len(prefix):] if station.startswith(prefix) else station
    launch.write_settings(client, station, role, state.stations_get(station, "parent", paths=paths),
                          state.stations_get(station, "project", paths=paths),
                          state.stations_get(station, "repo", paths=paths))
    return paths.station_file(station, "settings.json").read_text(encoding="utf-8")


def cmd_settings(argv: list, env: dict) -> int:
    """`tares factory settings <station> [role]`: print the settings file a station launches with."""
    paths = CrewPaths.from_env()
    try:
        state.crew_name(paths)
        client = TaresClient(paths, env)
        try:
            client.prime()
            text = settings(client, paths, list(argv[1:]))
        finally:
            client.close()
    except CrewError as e:
        sys.stdout.flush()
        print(e.line, file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0
