"""`tares factory allow [<role> [mcp|plugins [<name>... | --none]]]`: which of the person's MCP servers and Claude
Code plugins a station of each role keeps.

crew.json holds them beside the role's model: stations.<role>.mcp (names of servers in ~/.claude.json) and
stations.<role>.plugins (plugin ids as in enabledPlugins of ~/.claude/settings.json). A role with no list keeps
none: the Tares plugin and Claude Code's own tools only. A helper keeps its parent's role's lists. Nothing here
starts or changes a station; a station reads the lists when it launches.

With no word it shows the four roles, one line each. With a role it shows that role. `allow <role> mcp a b`
sets the list (the whole list, not an addition), `allow <role> mcp --none` empties it. A name Claude Code does
not know is kept and warned about. Only the person and the orchestrator set lists.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import claude, state
from .errors import CrewError
from .paths import ME, CrewPaths

ALLOW_ROLES = ("orchestrator", "reviewer", "releaser", "builder")
KINDS = ("mcp", "plugins")
USAGE = (f"usage: {ME} allow [<role> [mcp|plugins [<name>... | --none]]]\n"
         f"  roles: {', '.join(ALLOW_ROLES)}; mcp names a server of ~/.claude.json, plugins a plugin id")
SETTERS = ("orchestrator", "")


def known_mcp(home: str) -> list | None:
    """The MCP servers ~/.claude.json names (top level), or None when that file cannot be read."""
    doc = state.read_json(Path(home, ".claude.json"), None)
    if not isinstance(doc, dict):
        return None
    servers = doc.get("mcpServers")
    return sorted(servers) if isinstance(servers, dict) else []


def known_plugins(home: str) -> list | None:
    """The plugin ids ~/.claude/settings.json enables or lists in enabledPlugins, or None when it cannot be read."""
    doc = state.read_json(Path(home, ".claude", "settings.json"), None)
    if not isinstance(doc, dict):
        return None
    plugins = doc.get("enabledPlugins")
    return sorted(plugins) if isinstance(plugins, dict) else []


def unknown_names(paths: CrewPaths, role: str, home: str) -> dict:
    """{kind: [names]} the role lists that Claude Code does not have. A kind whose source cannot be read has none
    to report: not knowing is not a fault."""
    out = {}
    for kind, known in (("mcp", known_mcp(home)), ("plugins", known_plugins(home))):
        if known is None:
            continue
        missing = [n for n in state.ccfg_list("stations", role, kind, paths=paths) if n not in known]
        if missing:
            out[kind] = missing
    return out


def line(paths: CrewPaths, role: str) -> str:
    mcp, plugins = (state.ccfg_list("stations", role, k, paths=paths) for k in KINDS)
    return f"{role} keeps mcp {', '.join(mcp) or 'none'}; plugins {', '.join(plugins) or 'none'}"


def set_list(paths: CrewPaths, role: str, kind: str, names: list) -> None:
    cfg = state.read_json(paths.crew_cfg)
    if not isinstance(cfg, dict):
        raise CrewError(f"{paths.shown('crew.json')} is not an object")
    stations = cfg.setdefault("stations", {})
    row = stations.get(role)
    if row is None:
        row = stations[role] = {}
    if not isinstance(stations, dict) or not isinstance(row, dict):
        raise CrewError(f"stations.{role} in {paths.shown('crew.json')} is not an object")
    row[kind] = names
    state.write_json(paths.crew_cfg, cfg)


def run(args: list, env: dict, paths: CrewPaths, out) -> int:
    if not paths.crew_cfg.is_file():
        raise CrewError(f"no crew yet in {paths.raw}: {ME} crew up creates it")
    if not args:
        for r in ALLOW_ROLES:
            print(line(paths, r), file=out)
        return 0
    role = args[0]
    if role not in ALLOW_ROLES or (len(args) > 1 and args[1] not in KINDS):
        raise CrewError(USAGE)
    if len(args) <= 2:
        print(line(paths, role), file=out)
        return 0
    if env.get("FACTORY_ROLE", "") not in SETTERS:
        raise CrewError(f"only the person and the orchestrator set what a role keeps; ask {env.get('FACTORY_ORCHESTRATOR') or 'the orchestrator'}")
    kind, names = args[1], args[2:]
    if names == ["--none"]:
        names = []
    elif any(n.startswith("-") for n in names):
        raise CrewError(USAGE)
    names = list(dict.fromkeys(names))
    set_list(paths, role, kind, names)
    print(line(paths, role), file=out)
    missing = unknown_names(paths, role, claude._home(env)).get(kind, [])
    if missing:
        print(f"warning: Claude Code has no {'MCP server' if kind == 'mcp' else 'plugin'} named {', '.join(missing)}; kept anyway",
              file=sys.stderr)
    return 0


def cmd_allow(argv: list, env: dict) -> int:
    """argv starts with the word allow."""
    try:
        return run(list(argv[1:]), env, CrewPaths.from_env(), sys.stdout)
    except CrewError as e:
        print(getattr(e, "line", str(e)), file=sys.stderr)
        return 1
