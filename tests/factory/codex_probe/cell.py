"""A real Tares cell to throw away, for the probes that need one (TR-1596): the daemon and its MCP endpoint as
`tares up` and `tares mcp` start them, in a folder of their own, on free ports, with a login required.

Nothing of the person's Factory is read or written: the data folder, TARES_HOME and the ports are the probe's,
every FACTORY_*, TARES_* and TARESD_URL variable of the caller is dropped, and the keyring is switched off.
The cell is set to the Codex runtime, and a key bound to a person is made for the crew, as a person's login
would give one. `stop()` ends both processes; the caller removes the folder.

    cell = Cell(root)                       # root: an empty folder
    uid = cell.project("p1", repo_dir, crew="probe")   # a project handed over to the crew, with ticket T1
    cell.assign(uid, "T1", "p1-build-s1")
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

DROP = ("FACTORY_", "TARES_", "CLAUDE_PLUGIN_OPTION_", "NAVFLOW_")
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "codex-probe", "version": "1"}}}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def clean_env(base=None) -> dict:
    """`base` (this process's environment) without anything that names a Tares or a crew."""
    base = os.environ if base is None else base
    return {k: v for k, v in base.items() if not k.startswith(DROP) and k != "TARESD_URL"}


def tares_bin() -> str:
    mine = os.path.join(os.path.dirname(sys.executable), "tares")
    if not os.access(mine, os.X_OK):
        raise SystemExit("run this with the checkout's own interpreter (.venv/bin/python): its `tares` starts the cell")
    return mine


class CellError(Exception):
    pass


class Cell:
    def __init__(self, root: str):
        self.root = root
        self.port, self.mcp_port = free_port(), free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.mcp_url = f"http://127.0.0.1:{self.mcp_port}/mcp"
        self.root_token = "probe-root-" + os.urandom(8).hex()   # made for this run, good for nothing else
        self.env = clean_env()
        self.env.update(TARES_OTLP_GRPC_PORT="off", TARES_HOME=os.path.join(root, "tares-home"),
                        PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring", TARES_AUTH_TOKEN=self.root_token)
        self.daemon = self._spawn("daemon", ["up", "--host", "127.0.0.1", "--port", str(self.port), "--data-dir",
                                             os.path.join(root, "data"), "--edition", "factory"])
        self._wait("the cell", lambda: self.daemon.poll() is None
                   and "integrations" in (self.api("GET", "/health", key="").get("capabilities") or []), 90)
        self.mcp = None
        self.start_mcp()
        self.key = self.api("POST", "/api/keys", {"name": "crew", "scopes": ["admin"], "person_id": "local"})["secret"]
        self.api("PUT", "/api/factory/integrations/runtime", {"name": "codex"})
        self.api("PUT", "/api/crew/settings", {"autonomy": "L3-review", "releaser": False, "challenger": False,
                                                "builders_max": 1})

    def _spawn(self, name: str, argv: list):
        log = open(os.path.join(self.root, f"{name}.log"), "ab")
        return subprocess.Popen([tares_bin(), *argv], stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=self.env,
                                start_new_session=True)

    @staticmethod
    def _wait(what: str, cond, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if cond():
                    return
            except (CellError, OSError, ValueError):
                pass
            time.sleep(0.3)
        raise CellError(f"{what} did not come up in {seconds:.0f} s")

    def start_mcp(self) -> None:
        """The MCP endpoint, with a key of its own so that a request with no bearer is refused at once."""
        self.mcp = self._spawn("mcp", ["mcp", "--transport", "streamable-http", "--host", "127.0.0.1", "--port",
                                       str(self.mcp_port), "--taresd", self.url])

        def up():
            req = urllib.request.Request(self.mcp_url, data=json.dumps(INIT).encode(), method="POST", headers={
                "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.root_token}"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return "serverInfo" in r.read().decode("utf-8", "replace")
        self._wait("the MCP endpoint", lambda: self.mcp.poll() is None and up(), 60)

    def stop_mcp(self) -> None:
        self._end(self.mcp)
        self.mcp = None

    @staticmethod
    def _end(proc) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(15)

    def stop(self) -> None:
        self._end(self.mcp)
        self._end(self.daemon)

    def api(self, method: str, path: str, body=None, key=None, headers=None):
        """One request to the cell, with the root login unless `key` says otherwise ("" for none)."""
        key = self.root_token if key is None else key
        head = {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {}), **(headers or {})}
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                     method=method, headers=head)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise CellError(f"{method} {path}: {e.code} {e.read().decode(errors='replace')[:300]}") from None

    def project(self, name: str, repo_dir: str, crew: str, repo: str = "probe/repo") -> str:
        """A project with one ticket (T1), handed over to `crew` on `repo_dir`; its id."""
        uid = self.api("POST", "/api/projects", {"template": "custom", "kind": "software_factory", "name": name,
                                                 "objects": [], "goal": "A probe of a Codex station"}, key=self.key)["id"]
        base = f"/api/projects/{uid}"
        self.api("POST", base + "/tickets", {"title": "Say which station you are", "repo": repo}, key=self.key)
        self.api("POST", base + "/handover", {"repos": [{"path": repo_dir, "name": repo}], "crew": crew,
                                              "autonomy": "L3-review"}, key=self.key)
        return uid

    def assign(self, uid: str, ticket: str, holder: str) -> None:
        self.api("POST", f"/api/projects/{uid}/tickets/{ticket}/assign", {"holder": holder}, key=self.key)

    def ticket(self, uid: str, ticket: str) -> dict:
        return self.api("GET", f"/api/projects/{uid}/tickets/{ticket}", key=self.key)
