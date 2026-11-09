#!/usr/bin/env python3
"""Support for the launch tests (tests/test_crew_launch.py, the `launch_*` scenarios of parity.py): a fake
Tares, a copy of the kit, and a driver that runs one function of the launch code on either engine.

    launch_support.py <bash|python> <function> [arguments]

The driver runs in a throwaway crew (the environment parity.crew_env gives: the crew home is
<tmp>/crew, the fake Claude Code is CLAUDE_BIN). It starts a fake Tares on a free port of its own, makes
<tmp>/kit as a copy of claude-plugin (the same kit for both engines), and then:

  bash    sources the factory script, cut before its dispatcher, out of that kit and calls the bash function
  python  calls the function of tares.crew.launch with a TaresClient on the same fake Tares

Both print what the function prints and exit as it does, so the two can be compared. Functions:
write_settings, launch, boot_prompt, grant_rules (text on stdin), compact_window, kit_version, parent_role,
level_rank, level_text, check_model, plugin_flags, idle_wake_on, idle_wake_note, and the whole command `spawn` (bash's cmd_spawn, or cmd_spawn of
tares.crew.stations). The word {TMP} in an
argument is the temp folder.

Environment: LAUNCH_WORLD is JSON for the fake Tares (settings, projects, grants, token, health,
grants_status); LAUNCH_SETUP is JSON for the crew home, made on the first call (crew: keys merged into
crew.json, stations: stations.json, token: the crew's key file; a later call's crew: keys are merged
into the crew.json again). {TMP} in either is the temp folder.

Standard library only.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PLUGIN = ROOT / "claude-plugin"
CUT = "cmd=${1:-help}; shift || true"  # the line of bin/factory where the commands begin

DEFAULT_WORLD = {
    "settings": {"autonomy": "L4-ship", "releaser": False, "release_profile": "kind",
                 "prod_pattern": "(^|[-_.])(prod|production)([-_.]|$)", "challenger": False, "builders_max": 2},
    "projects": [],
    "grants": {"all": "", "projects": {}},
    "token": "",             # when set, Tares wants this key as a Bearer token
    "health": True,
    "mcp": True,             # does POST /mcp answer `initialize` like an MCP server (the spawn command wants one)
    "settings_status": 200,  # the status of GET /api/crew/settings
    "grants_status": 200,    # the status of GET /api/grants
}


class FakeTares:
    """A Tares that answers /health, /api/crew/settings, /api/crew/projects and /api/grants from a world."""

    def __init__(self, world: dict | None = None):
        self.world = {**copy.deepcopy(DEFAULT_WORLD), **(world or {})}
        self.log: list = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_a):
                pass

            def _send(self, code, body):
                raw = (body if isinstance(body, str) else json.dumps(body)).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                outer.log.append({"path": self.path, "auth": self.headers.get("Authorization"), "post": True})
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.path == "/mcp" and outer.world["mcp"]:
                    return self._send(200, {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "fake"}}})
                return self._send(404, {"detail": "no"})

            def do_GET(self):
                w = outer.world
                u = urlparse(self.path)
                outer.log.append({"path": self.path, "auth": self.headers.get("Authorization")})
                if u.path == "/health":
                    return self._send(200 if w["health"] else 503, {"status": "ok"})
                if w["token"] and self.headers.get("Authorization") != f"Bearer {w['token']}":
                    return self._send(401, {"detail": "unauthorized"})
                if u.path == "/api/crew/settings":
                    return self._send(w["settings_status"], {"settings": w["settings"]})
                if u.path == "/api/crew/projects":
                    return self._send(200, {"projects": w["projects"]})
                if u.path == "/api/grants":
                    if w["grants_status"] != 200:
                        return self._send(w["grants_status"], {"detail": "grants"})
                    q = parse_qs(u.query)
                    if "project" in q:
                        mine = w["grants"]["projects"]
                        if q["project"][0] not in mine:
                            return self._send(404, {"detail": "unknown project"})
                        return self._send(200, {"all": w["grants"]["all"], "project": mine[q["project"][0]]})
                    return self._send(200, {"all": w["grants"]["all"]})
                return self._send(404, {"detail": "no"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------------------------
# The throwaway crew

def crew_template() -> dict:
    return json.loads((PLUGIN / "templates" / "crew.json").read_text(encoding="utf-8"))


def merge(base, over):
    """jq's `*`: objects merge key by key, anything else is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = merge(base[k], v) if k in base else v
        return out
    return over


def make_kit(tmp: Path) -> Path:
    """<tmp>/kit: a copy of claude-plugin, with bin/factory-lib.sh, the factory script cut before its
    dispatcher (the functions of the bash engine, ready to source). Made once."""
    kit = tmp / "kit"
    if not kit.exists():
        shutil.copytree(PLUGIN, kit, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        text = (PLUGIN / "bin" / "factory").read_text(encoding="utf-8")
        head = text.split(CUT)[0]
        if head == text:
            raise SystemExit("launch_support: bin/factory has no dispatcher line to cut at")
        (kit / "bin" / "factory-lib.sh").write_text(head, encoding="utf-8", newline="\n")
    return kit


def make_crew(tmp: Path, setup: dict) -> None:
    """The crew home of a test: crew.json (the template and setup['crew']), stations.json, the key file."""
    home = tmp / "crew"
    if (home / "crew.json").exists():  # a later step: only its crew.json changes are applied
        if setup.get("crew"):
            cfg = merge(json.loads((home / "crew.json").read_text(encoding="utf-8")), setup["crew"])
            (home / "crew.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        return
    for d in ("stations", "reports", "worktrees"):
        (home / d).mkdir(parents=True, exist_ok=True)
    cfg = merge(crew_template(), {"human": {"name": "Pat"}, "authorized": {"by": "Pat", "at": "2026-01-01"}})
    cfg = merge(cfg, setup.get("crew") or {})
    (home / "crew.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    (home / "stations.json").write_text(json.dumps(setup.get("stations") or {}, indent=2) + "\n", encoding="utf-8")
    if setup.get("token"):
        (home / ".tares-token").write_text(setup["token"] + "\n", encoding="utf-8")
    for d in setup.get("mkdirs") or []:
        Path(d).mkdir(parents=True, exist_ok=True)
    if setup.get("halt"):  # the crew is halted, for this reason (HALT holds {at, reason}, compact)
        (home / "HALT").write_text(json.dumps({"at": "2026-01-01T00:00:00Z", "reason": setup["halt"]}, separators=(",", ":")) + "\n",
                                   encoding="utf-8")
    for d in setup.get("gitrepos") or []:  # a folder that is a git repository
        Path(d).mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", d], check=True)


def _sub(text: str, tmp: Path) -> str:
    """{TMP} as the plain path, for an argument."""
    return text.replace("{TMP}", str(tmp))


def _sub_json(text: str, tmp: Path) -> str:
    """{TMP} as the path escaped for the inside of a JSON string (a Windows path has backslashes)."""
    return text.replace("{TMP}", json.dumps(str(tmp))[1:-1])


# ---------------------------------------------------------------------------------------------
# The two engines

BASH_FUNCTIONS = {"write_settings", "launch", "boot_prompt", "grant_rules", "compact_window", "kit_version",
                  "parent_role", "level_rank", "level_text", "check_model", "plugin_flags", "idle_wake_on",
                  "idle_wake_note", "spawn"}
NEEDS_TARES = {"write_settings", "launch", "boot_prompt", "idle_wake_on", "spawn"}


def run_bash(fn: str, args: list, env: dict, kit: Path) -> int:
    """Source the cut factory script from the kit and call the bash function."""
    lib = kit / "bin" / "factory-lib.sh"
    prime = "prime; " if fn in NEEDS_TARES else ""
    # the script reads its first argument to tell a command from help; "x" is not a command it knows
    script = f'set -euo pipefail; source "$1" x; shift; {prime}{fn} "$@"'
    if fn == "idle_wake_on":
        script = script.replace('idle_wake_on "$@"', 'idle_wake_on')
    if fn == "spawn":
        script = script.replace('spawn "$@"', 'cmd_spawn "$@"')
    p = subprocess.run(["bash", "-c", script, "bash", str(lib), *args], env=env, stdin=None)
    return p.returncode


def run_python(fn: str, args: list) -> int:
    from tares.crew import launch as L
    from tares.crew import state
    from tares.crew.client import TaresClient
    from tares.crew.errors import CrewError
    from tares.crew.paths import ME, CrewPaths
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", newline="\n")
    paths = CrewPaths.from_env()
    client = TaresClient(paths)
    try:
        if fn in NEEDS_TARES:
            client.prime()
        if fn == "write_settings":
            print(L.write_settings(client, *args))
        elif fn == "launch":
            L.launch(client, *args)
        elif fn == "boot_prompt":
            print(L.boot_prompt(client, *args))
        elif fn == "grant_rules":
            for rule in L.grant_rules(sys.stdin.read(), *args):
                print(rule)
        elif fn == "compact_window":
            print(L.compact_window(paths, client.say))
        elif fn == "kit_version":
            print(L.kit_version(*args))
        elif fn == "parent_role":
            print(L.parent_role(args[0], paths))
        elif fn == "level_rank":
            rank = L.level_rank(args[0])
            if rank is None:
                return 1
            print(rank)
        elif fn == "level_text":
            print(L.level_text(args[0]))
        elif fn == "check_model":
            L.check_model(*args)
        elif fn == "plugin_flags":
            print("\n".join(L.plugin_flags()))
        elif fn == "idle_wake_on":
            return 0 if L.idle_wake_on(client) else 1
        elif fn == "idle_wake_note":
            print(L.idle_wake_note())
        elif fn == "spawn":
            from tares.crew import stations
            return stations.cmd_spawn(["spawn", *args], dict(os.environ))
        else:
            print(f"launch_support: no function {fn}", file=sys.stderr)
            return 2
    except CrewError as e:
        print(f"{ME}: {e}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def main(argv: list) -> int:
    if len(argv) < 2 or argv[0] not in ("bash", "python") or argv[1] not in BASH_FUNCTIONS:
        print(__doc__, file=sys.stderr)
        return 2
    engine, fn, args = argv[0], argv[1], argv[2:]
    tmp = Path(os.environ["FACTORY_CREW_HOME"]).parent
    args = [_sub(a, tmp) for a in args]
    setup = json.loads(_sub_json(os.environ.get("LAUNCH_SETUP") or "{}", tmp))
    world = json.loads(_sub_json(os.environ.get("LAUNCH_WORLD") or "{}", tmp))
    kit = make_kit(tmp)
    make_crew(tmp, setup)
    with FakeTares(world) as tares:
        mcp = tares.url + "/mcp"
        env = {**os.environ, "TARESD_URL": tares.url, "TARES_PLUGIN_DIR": str(kit), "TARES_FACTORY_MCP_URL": mcp}
        if engine == "bash":
            return run_bash(fn, args, env, kit)
        os.environ.update(TARESD_URL=tares.url, TARES_PLUGIN_DIR=str(kit), TARES_FACTORY_MCP_URL=mcp)
        sys.path.insert(0, str(ROOT))
        return run_python(fn, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
