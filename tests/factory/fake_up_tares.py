#!/usr/bin/env python3
"""A small stand-in for a Tares and its MCP endpoint, for the parity scenarios of `crew up`.

It answers what crew up and the station launches ask, from memory, on one free port of 127.0.0.1:

    GET  /health                    ok, with the capabilities list (default: integrations, merge_queue)
    GET  /api/crew/settings         the crew settings saved so far (a flat object)
    PUT  /api/crew/settings         merge the body into them
    GET  /api/grants                {"all": "- \\"<words>\\" (<date>)\\n..."}
    POST /api/grants                {words, scope: "all"}: one more line
    GET  /api/people                {"people": [...]}
    GET  /api/crew/projects         {"projects": [...]} (none unless the scenario hands some over)
    GET  /api/desk                  {"desk": []}
    POST /mcp                       an MCP `initialize` answer (it has serverInfo)

With `token` set it is a Tares with auth on: every route but /health and /mcp answers 401 without `Authorization: Bearer <token>`.
/mcp is the shared MCP endpoint, a server of its own that answers `initialize` to anyone; `mcp_auth` keeps the
Authorization header of each probe it got, for a scenario to see that the probe carried the key.
With `projects` (the documents GET /api/crew/projects lists; "{tmp}" in a repo path is the folder the scenario runs in)
it also answers GET /api/projects/<id> and GET /api/grants?project=<id>, and keeps a project grant (POST /api/grants
with scope "project"). `project_grants` is {project id: [grant lines]}.

Every write (PUT, POST except /mcp) is kept in order in <dir>/writes.jsonl as {method, path, body}, so a
scenario can compare what was saved. Anything else answers 404. Standard library only; it runs in a
thread of the process that starts it, so it works on Windows too. This is a test double, not the product:
the milestone runs still use a real daemon.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FakeTares:
    def __init__(self, directory, settings=None, people=None, grants=None, capabilities=None,
                 token="", projects=None, project_grants=None, health=None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.writes = self.dir / "writes.jsonl"
        self.writes.write_text("", encoding="utf-8")
        self.settings = dict(settings or {})
        self.people = list(people if people is not None else [{"id": "local", "name": "Local"}])
        self.grants = list(grants or [])
        # what /health lists: crew up wants "integrations" and "merge_queue" (need_capability); capabilities=[] is an old Tares
        self.capabilities = list(["integrations", "merge_queue"] if capabilities is None else capabilities)
        self.health = health                # None: the short /health body; a dict: that whole body (an old cell, or the real daemon's)
        self.token = token                  # "": no auth
        self.mcp_auth: list = []            # the Authorization header (or None) of each POST /mcp
        self.projects = json.loads(json.dumps(projects or []).replace("{tmp}", json.dumps(str(self.dir.parent))[1:-1]))
        self.project_grants = {k: list(v) for k, v in (project_grants or {}).items()}
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):  # silence
                pass

            def _send(self, code, doc):
                body = json.dumps(doc).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n).decode("utf-8") if n else ""
                try:
                    return json.loads(raw) if raw else None
                except ValueError:
                    return raw

            def _refused(self, path):
                """True (after answering 401) when auth is on and the request carries another key."""
                if outer.token and path != "/health" and self.headers.get("Authorization") != f"Bearer {outer.token}":
                    self._send(401, {"detail": "unauthorized"})
                    return True
                return False

            def do_GET(self):
                path = self.path.split("?", 1)[0]
                if self._refused(path):
                    return
                with outer.lock:
                    if path == "/health":
                        if outer.health is not None:
                            return self._send(200, dict(outer.health))
                        return self._send(200, {"status": "ok", "capabilities": list(outer.capabilities)})
                    if path == "/api/crew/settings":
                        return self._send(200, dict(outer.settings))
                    if path == "/api/grants":
                        allg = "".join(line + "\n" for line in outer.grants).rstrip("\n")
                        if "project=" in self.path:
                            from urllib.parse import unquote
                            ref = unquote(self.path.split("project=", 1)[1].split("&", 1)[0])
                            found = [p for p in outer.projects if ref in (p.get("id"), p.get("name"))]
                            if not found:
                                return self._send(404, {"detail": "unknown project"})
                            pid = found[0]["id"]
                            mine = outer.project_grants.get(pid, [])
                            return self._send(200, {"all": allg, "project": "".join(line + "\n" for line in mine).rstrip("\n")})
                        return self._send(200, {"all": allg})
                    if path == "/api/people":
                        return self._send(200, {"people": outer.people})
                    if path == "/api/crew/projects":
                        return self._send(200, {"projects": outer.projects})
                    if path.startswith("/api/projects/"):
                        for p in outer.projects:
                            if path == "/api/projects/" + p.get("id", ""):
                                return self._send(200, p)
                    if path == "/api/desk":
                        return self._send(200, {"desk": []})
                self._send(404, {"error": "not found"})

            def do_PUT(self):
                body = self._body()
                path = self.path.split("?", 1)[0]
                if self._refused(path):
                    return
                with outer.lock:
                    outer._log("PUT", path, body)
                    if path == "/api/crew/settings" and isinstance(body, dict):
                        outer.settings.update(body)
                        return self._send(200, dict(outer.settings))
                self._send(404, {"error": "not found"})

            def do_POST(self):
                body = self._body()
                path = self.path.split("?", 1)[0]
                if path.startswith("/mcp"):
                    outer.mcp_auth.append(self.headers.get("Authorization"))
                    return self._send(200, {"jsonrpc": "2.0", "id": 1, "result": {
                        "protocolVersion": "2025-03-26", "capabilities": {},
                        "serverInfo": {"name": "fake-tares", "version": "0"}}})
                if self._refused(path):
                    return
                with outer.lock:
                    outer._log("POST", path, body)
                    if path == "/api/grants" and isinstance(body, dict) and body.get("words"):
                        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        line = f'- "{body["words"]}" ({today})'
                        if body.get("scope") == "project" and body.get("project"):
                            outer.project_grants.setdefault(body["project"], []).append(line)
                        else:
                            outer.grants.append(line)
                        return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _log(self, method, path, body):
        with open(self.writes, "a", encoding="utf-8") as f:
            f.write(json.dumps({"method": method, "path": path, "body": body}, sort_keys=True) + "\n")

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.url}/mcp"

    def start(self) -> "FakeTares":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def written(self) -> list:
        return [json.loads(x) for x in self.writes.read_text(encoding="utf-8").splitlines() if x.strip()]
