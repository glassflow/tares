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
level_rank, level_text, check_model, plugin_flags, idle_wake_on, idle_wake_note, pace_round (the pace pass of one
watch round), pace_down (what the revive step asks of the pace for stations that are down or up), and the whole commands `spawn` (bash's cmd_spawn, or cmd_spawn of
tares.crew.stations), `revive` and `restart` (cmd_revive and cmd_restart, or those of tares.crew.lifecycle). The word {TMP} in an
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
    "routes": {},            # any other GET: "<path>[?query]" -> [status, body] (the full path with its query wins over the path)
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
                hit = w["routes"].get(self.path) or w["routes"].get(u.path)
                if hit:
                    return self._send(hit[0], hit[1])
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
                  "idle_wake_note", "spawn", "revive", "restart", "pace_round", "pace_down"}
NEEDS_TARES = {"write_settings", "launch", "boot_prompt", "idle_wake_on", "spawn", "revive", "restart", "pace_round",
               "pace_down"}

# The pace pass of one watch round, as the bash watch loop runs it: the reading and the tidy at the top of the
# round, then for each active station that is up the plan and the send (the revive and the regrant before it
# belong to other steps).
BASH_PACE_ROUND = '''
pace_read || true
pace_tidy || true
for s in $(jq -r 'to_entries[] | select(.value.status == "active") | .key' "$STATIONS" 2>/dev/null); do
  case "$(stations_get "$s" '.role')" in
    helper) continue ;;
    builder) [ -f "$CREW_HOME/stations/$s.done" ] && continue ;;
  esac
  up=$(is_alive "$s" && echo yes || echo no) || { say "skipping $s this round"; continue; }
  [ "$up" = yes ] || continue
  if pace_plan "$s"; then pace_send; fi
done
'''

# What the watch loop asks of the pace around a revive, for each "<station>:<what>" argument: `up` (the station
# runs: forget it), `ok` or `fail` (it is down; a closed limit holds it down, else it is revived, and the revive
# worked or not). The revive itself is not made: the prompt it would get is printed.
BASH_PACE_DOWN = '''
pace_read || true
pace_tidy || true   # the top of the round, which the revive step runs after
for a in "$@"; do
  s=${a%%:*}; o=${a##*:}
  if [ "$o" = up ]; then [ ! -f "$PACE_FILE" ] || pace_revived "$s"; continue; fi
  if pace_closed; then pace_hold_down "$s"; continue; fi
  fresh=""; pprompt=""
  if pace_stopped "$s"; then
    if pace_tried "$s"; then fresh=--fresh; pprompt=$(pace_fresh_prompt "$s"); else pprompt=$(pace_resume_prompt); fi
  fi
  echo "revive $s${fresh:+ $fresh}: $pprompt"
  if [ "$o" = ok ]; then pace_revived "$s"
  else [ -z "$pprompt" ] || [ -n "$fresh" ] || pace_mark_tried "$s"; fi
done
'''


def run_bash(fn: str, args: list, env: dict, kit: Path) -> int:
    """Source the cut factory script from the kit and call the bash function."""
    lib = kit / "bin" / "factory-lib.sh"
    prime = "prime; " if fn in NEEDS_TARES else ""
    # the script reads its first argument to tell a command from help; "x" is not a command it knows
    script = f'set -euo pipefail; source "$1" x; shift; {prime}{fn} "$@"'
    if fn == "idle_wake_on":
        script = script.replace('idle_wake_on "$@"', 'idle_wake_on')
    if fn in ("spawn", "revive", "restart"):
        script = script.replace(f'{fn} "$@"', f'cmd_{fn} "$@"')
    if fn == "pace_round":
        script = script.replace('pace_round "$@"', BASH_PACE_ROUND)
    if fn == "pace_down":
        script = script.replace('pace_down "$@"', BASH_PACE_DOWN)
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
        elif fn == "pace_round":
            from tares.crew import pace
            pace.step(paths)
        elif fn == "pace_down":
            from tares.crew import pace
            seam = pace.Seam()
            for a in args:
                s, _, o = a.rpartition(":")
                if o == "up":
                    seam.revived(paths, s)
                    continue
                if seam.closed(paths):
                    seam.hold_down(paths, s)
                    continue
                fresh, pprompt = "", ""
                if seam.stopped(paths, s):
                    if seam.tried(paths, s):
                        fresh, pprompt = " --fresh", seam.fresh_prompt(paths, s)
                    else:
                        pprompt = seam.resume_prompt(paths)
                print(f"revive {s}{fresh}: {pprompt}")
                if o == "ok":
                    seam.revived(paths, s)
                elif pprompt and not fresh:
                    seam.mark_tried(paths, s)
        elif fn == "spawn":
            from tares.crew import stations
            return stations.cmd_spawn(["spawn", *args], dict(os.environ))
        elif fn in ("revive", "restart"):
            from tares.crew import lifecycle
            return getattr(lifecycle, f"cmd_{fn}")([fn, *args], dict(os.environ))
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
        env = {**os.environ, "TARESD_URL": tares.url, "TARES_PLUGIN_DIR": str(kit)}
        if fn == "spawn":  # the spawn command wants the crew's MCP endpoint, which the settings checks leave at its default
            env["TARES_FACTORY_MCP_URL"] = tares.url + "/mcp"
        if engine == "bash":
            return run_bash(fn, args, env, kit)
        os.environ.update(TARESD_URL=tares.url, TARES_PLUGIN_DIR=str(kit))
        if fn == "spawn":
            os.environ["TARES_FACTORY_MCP_URL"] = tares.url + "/mcp"
        sys.path.insert(0, str(ROOT))
        return run_python(fn, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
