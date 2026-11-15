#!/usr/bin/env python3
"""The small fakes the doctor scenarios of tests/factory/parity.py run against: a Tares that answers the
few routes `tares factory doctor` asks, a `gh`, and a `tares` command. Standard library only.

    World         what the fake Tares answers; a scenario's `before` hook changes it between two steps
                  (a PUT of the crew settings is kept, unless `keep` is False; `broken` makes every GET but
                  /health a 500; `routes` adds GET paths)
    serve(world)  start it on a free port in a thread: (server, base url)
    gh_main()     the fake gh: `auth status` is signed in; `repo view` names the folder's repo, with WRITE
                  access unless the folder's name starts with ro (READ) or nogh (no GitHub repo)
    tares_main()  the fake tares: `--version`, and `hook <name>` exits 0
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_launch import launch_answer  # noqa: E402
from fake_rules import RULES_PATH, rules_answer  # noqa: E402

SETTINGS = {"autonomy": "L4-ship", "releaser": False, "challenger": False, "builders_max": 2,
            "idle_wake": False, "pace": False, "claude_update": True, "kit": {"frozen": None}}


class World:
    def __init__(self, token: str = "", projects=None, settings=None):
        self.token = token                  # "" = no auth
        self.projects = projects or []
        self.settings = dict(SETTINGS, **(settings or {}))
        self.mcp = True
        self.keep = True                    # False: a PUT of the settings is answered but not kept
        self.broken = False                 # True: a Tares that is up but cannot answer, every GET but /health is a 500
        self.capabilities = ["guardrail_rules", "launch_spec"]   # what /health lists: a station start asks for the launch route (TR-870)
        self.routes = {}                    # more GETs: path -> answer (200); /api/grants is one the watch asks
        self.log = []


def _handler(world: World):
    class Fake(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def _json(self, code, body):
            raw = (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _go(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            path = urlparse(self.path).path
            world.log.append((self.command, path, self.headers.get("Authorization")))
            if path == "/health":
                return self._json(200, {"status": "ok", "capabilities": list(world.capabilities)})
            if world.token and self.headers.get("Authorization") != f"Bearer {world.token}":
                return self._json(401, {"detail": "unauthorized"})
            if world.broken and self.command == "GET":
                return self._json(500, {"detail": "broken"})
            if path == "/mcp":
                if not world.mcp:
                    return self._json(404, {})
                return self._json(200, '{"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"tares"}}}')
            if path == "/api/crew/settings":
                if self.command == "PUT":
                    # the real PUT answers the whole settings (the kit at the top level) and keeps what it was given
                    if world.keep:
                        world.settings.update(json.loads(raw.decode("utf-8") or "{}"))
                    return self._json(200, world.settings)
                return self._json(200, {"settings": world.settings})
            if path == "/api/crew/projects":
                return self._json(200, {"projects": world.projects})
            if self.command == "GET" and path in world.routes:
                return self._json(200, world.routes[path])
            if self.command == "GET" and "/launch" in path:
                grants = (world.routes.get("/api/grants") or {}).get("all", "")
                hit = launch_answer(self.path, self.headers, world.projects,
                                    lambda name, role: rules_answer("", role, name, grants, ""),
                                    world.settings.get("autonomy", "L4-ship"), f"http://{self.headers.get('Host')}")
                if hit:
                    return self._json(*hit)
            if self.command == "GET" and path == RULES_PATH:
                if not self.headers.get("X-Tares-Station"):   # the real route: a key alone, without a station, is refused
                    return self._json(403, {"detail": "this needs an admin or a crew station"})
                q = parse_qs(urlparse(self.path).query)
                grants = (world.routes.get("/api/grants") or {}).get("all", "")
                return self._json(200, rules_answer((q.get("station") or [""])[0], (q.get("role") or ["builder"])[0],
                                                    (q.get("project") or [""])[0], grants, ""))
            self._json(404, {})

        do_GET = do_POST = do_PUT = do_DELETE = _go  # noqa: N815

        def log_message(self, *a):
            pass

    return Fake


def serve(world: World):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(world))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def gh_main(argv: list) -> int:
    if argv[:2] == ["auth", "status"]:
        return 0
    if argv[:2] == ["repo", "view"]:
        base = os.path.basename(os.getcwd())
        if base.startswith("nogh"):
            return 1
        print(f"o/{base} {'READ' if base.startswith('ro') else 'WRITE'}")
        return 0
    return 1


def tares_main(argv: list) -> int:
    if argv[:1] == ["--version"]:
        print("tares 99.0.0")
        return 0
    if argv[:1] == ["hook"]:
        return 0
    return 2


if __name__ == "__main__":
    which = sys.argv[1]
    sys.exit({"gh": gh_main, "tares": tares_main}[which](sys.argv[2:]))
