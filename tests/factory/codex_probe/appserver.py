"""A small client for `codex app-server`, for the by-hand Codex probes (TR-1576).

The app-server speaks JSON-RPC without the "jsonrpc" field, one JSON document per line on stdio.
Three kinds of message arrive: a response (has `id` and `result` or `error`), a notification (has
`method`, no `id`) and a server request (has `method` and `id`, and waits for our answer, as an
approval does). Every message in either direction is appended to a transcript, so a probe's
evidence is the wire itself.

Two ways in: `AppServer.spawn` starts the app-server as a child and owns its stdio; `AppServer.connect`
joins an app-server that listens on a Unix socket (`--listen unix://PATH`) and lives on its own. On the
socket the same JSON documents travel as WebSocket text frames, one per frame.

Standard library only.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import socket
import struct
import subprocess
import threading
import time


class AppServerError(Exception):
    pass


class _WebSocketPipe:
    """Just enough WebSocket (RFC 6455) over a Unix socket to carry JSON text frames to an app-server that
    listens on `unix://PATH`: the upgrade handshake, masked text frames out, text frames in, pong for ping.
    It stands in for a child process: `.stdin.write(line)` sends one frame, iterating `.stdout` yields one
    line per frame."""

    def __init__(self, path: str, timeout: float = 15):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AppServerError("the socket closed during the WebSocket handshake")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise AppServerError("no WebSocket upgrade: " + head[:200].decode("latin-1"))
        self._buf = head.split(b"\r\n\r\n", 1)[1]
        self.sock.settimeout(None)
        self._wlock = threading.Lock()
        self.stdin, self.stdout, self.returncode = self, self, None

    def _frame(self, opcode: int, payload: bytes) -> None:
        n, mask = len(payload), os.urandom(4)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._wlock:
            self.sock.sendall(head + mask + body)

    def write(self, data: bytes) -> None:
        try:
            self._frame(0x1, data.rstrip(b"\n"))
        except OSError as e:
            raise BrokenPipeError(str(e)) from e

    def flush(self) -> None:
        pass

    def _take(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def __iter__(self):
        message = b""
        try:
            while True:
                b0, b1 = self._take(2)
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._take(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._take(8))[0]
                mask = self._take(4) if b1 & 0x80 else None
                payload = self._take(n)
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                opcode = b0 & 0x0F
                if opcode == 0x9:
                    self._frame(0xA, payload)
                elif opcode == 0x8:
                    return
                elif opcode in (0x0, 0x1, 0x2):
                    message += payload
                    if b0 & 0x80:
                        yield message + b"\n"
                        message = b""
        except (EOFError, OSError):
            return

    def close(self) -> None:
        """Hang up without a closing handshake, as a controller that died would."""
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        self.returncode = 0

    def kill(self) -> None:
        self.close()

    def wait(self, timeout=None) -> int:
        return 0


class AppServer:
    def __init__(self, proc, transcript_path: str | None = None, name: str = "client"):
        self.proc = proc
        self.name = name
        self._next = 0
        self._lock = threading.Lock()
        self._responses: dict[int, dict] = {}
        self._cond = threading.Condition()
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.log: list[dict] = []
        self._transcript = open(transcript_path, "a", encoding="utf-8") if transcript_path else None
        self.on_server_request = None  # fn(msg) -> result dict, or None to leave it unanswered
        self.closed = False
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    # -- starting ---------------------------------------------------------------------------------------------
    @classmethod
    def spawn(cls, codex: str, args: list[str] | None = None, env: dict | None = None, cwd: str | None = None,
              transcript_path: str | None = None, stderr_path: str | None = None, new_session: bool = False):
        stderr = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
        proc = subprocess.Popen([codex, "app-server", *(args or [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=stderr, env=env, cwd=cwd, start_new_session=new_session)
        return cls(proc, transcript_path)

    @classmethod
    def connect(cls, sock: str, transcript_path: str | None = None, name: str = "client"):
        """A controller of an app-server that listens on a Unix socket and lives on its own."""
        return cls(_WebSocketPipe(sock), transcript_path, name=name)

    # -- wire -------------------------------------------------------------------------------------------------
    def _record(self, direction: str, msg: dict) -> None:
        entry = {"t": round(time.time(), 3), "dir": direction, "client": self.name, "msg": msg}
        self.log.append(entry)
        if self._transcript:
            self._transcript.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._transcript.flush()

    def _send(self, msg: dict) -> None:
        self._record("out", msg)
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, ValueError) as e:
                raise AppServerError(f"app-server is gone: {e}") from e

    def _read(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._record("in-bad", {"line": line[:500]})
                continue
            self._record("in", msg)
            if "method" in msg and "id" in msg:  # a server request
                self.events.put(msg)
                handler = self.on_server_request
                if handler:
                    try:
                        result = handler(msg)
                    except Exception as e:  # a probe bug must not hang the server
                        result = None
                        self._record("handler-error", {"error": repr(e)})
                    if result is not None:
                        self._send({"id": msg["id"], "result": result})
            elif "method" in msg:
                self.events.put(msg)
            elif "id" in msg:
                with self._cond:
                    self._responses[msg["id"]] = msg
                    self._cond.notify_all()
        self.closed = True
        with self._cond:
            self._cond.notify_all()

    def request(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        with self._lock:
            self._next += 1
            rid = self._next
        msg = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        deadline = time.time() + timeout
        with self._cond:
            while rid not in self._responses:
                left = deadline - time.time()
                if left <= 0 or self.closed:
                    raise AppServerError(f"no answer to {method} ({'closed' if self.closed else 'timeout'})")
                self._cond.wait(min(left, 0.5))
            resp = self._responses.pop(rid)
        if "error" in resp:
            raise AppServerError(f"{method}: {json.dumps(resp['error'])[:600]}")
        return resp.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def respond(self, request_id, result: dict) -> None:
        self._send({"id": request_id, "result": result})

    # -- conversation -----------------------------------------------------------------------------------------
    def initialize(self, name: str = "tares-probe", experimental: bool = True) -> dict:
        params = {"clientInfo": {"name": name, "title": "Tares Codex probe", "version": "0"}}
        if experimental:
            params["capabilities"] = {"experimentalApi": True}
        result = self.request("initialize", params)
        self.notify("initialized")
        return result

    def wait_for(self, pred, timeout: float = 180, on_event=None) -> dict | None:
        """The first event `pred` accepts, or None on timeout or when the server went away. Every event seen
        on the way goes to `on_event` (so a probe can act in the middle of a turn)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ev = self.events.get(timeout=0.25)
            except queue.Empty:
                if self.closed and self.events.empty():
                    return None
                continue
            if on_event:
                on_event(ev)
            if pred(ev):
                return ev
        return None

    def run_turn(self, thread_id: str, text: str, timeout: float = 240, on_event=None, **overrides) -> dict:
        """Start a turn and wait for its turn/completed. Returns {"turn_id", "status", "completed"}."""
        params = {"threadId": thread_id, "input": [{"type": "text", "text": text}], **overrides}
        started = self.request("turn/start", params)
        turn_id = (started.get("turn") or {}).get("id")
        done = self.wait_for(lambda e: e.get("method") == "turn/completed"
                             and ((e.get("params") or {}).get("turn") or {}).get("id") in (turn_id, None),
                             timeout=timeout, on_event=on_event)
        status = (((done or {}).get("params") or {}).get("turn") or {}).get("status") if done else "no-completion"
        return {"turn_id": turn_id, "status": status, "completed": done}

    def close(self, kill: bool = False, wait: float = 5) -> int | None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        if kill:
            self.proc.kill()
        try:
            return self.proc.wait(wait)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(5)


def items(log: list[dict], method: str = "item/completed", kind: str | None = None) -> list[dict]:
    """The `item` of every item/started or item/completed notification in a client's log, by item type."""
    out = []
    for entry in log:
        msg = entry.get("msg") or {}
        if entry.get("dir") == "in" and msg.get("method") == method:
            item = (msg.get("params") or {}).get("item") or {}
            if kind is None or item.get("type") == kind:
                out.append(item)
    return out


def notifications(log: list[dict], method: str) -> list[dict]:
    return [e["msg"].get("params") or {} for e in log
            if e.get("dir") == "in" and (e.get("msg") or {}).get("method") == method]


def codex_bin() -> str:
    path = os.environ.get("TARES_PROBE_CODEX", "codex")
    return path
