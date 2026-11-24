"""Talking to a station's Codex app-server on its socket (TR-1592).

`codex app-server --listen unix://PATH` speaks JSON-RPC without the "jsonrpc" field. On the socket each JSON
document is one WebSocket text frame (RFC 6455). This is just enough of both to ask a question and read its
answer: the upgrade handshake, masked text frames out, frames in, a pong for a ping. A notification that
arrives while an answer is awaited is kept (the last KEPT of them) and given out by `next`, which also waits
for the one after; a request of the server's own is passed over.

Measured against Codex 0.171.0 (TR-1576, tests/factory/codex_probe/appserver.py, the fuller client the
probes use). Standard library only.
"""
from __future__ import annotations

import base64
import collections
import json
import os
import socket
import struct
import time


class WireError(Exception):
    """The app-server could not be reached, hung up, or answered with an error."""


KEPT = 500   # notifications kept while nobody reads them


class Wire:
    """One connection to an app-server. `Wire(path)` connects and upgrades; `request` asks and waits;
    `notify` tells; `next` gives the next notification; `close` hangs up. `timeout` is seconds for the
    connection and for each answer.

    `path` may be a link to the socket: Codex 0.171.0 on macOS keeps the socket in a folder of its own and
    makes the path it was given a link. The link is followed before connecting, since the address of a Unix
    socket holds about a hundred bytes and the path given may be longer."""

    def __init__(self, path: str, timeout: float = 15):
        path = os.path.realpath(path)
        self.timeout = timeout
        self._next = 0
        self._buf = b""
        self._part = b""
        self._kept = collections.deque(maxlen=KEPT)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(path)
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise WireError("the app-server hung up during the handshake")
                head += chunk
        except OSError as e:
            self.sock.close()
            raise WireError(f"cannot reach the app-server on {path}: {e}") from None
        except WireError:
            self.sock.close()
            raise
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            self.sock.close()
            raise WireError("the socket does not answer as an app-server: " + head[:120].decode("latin-1").strip())
        self._buf = head.split(b"\r\n\r\n", 1)[1]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # ---- frames ----

    def _frame(self, opcode: int, payload: bytes) -> None:
        n, mask = len(payload), os.urandom(4)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _fill(self, n: int) -> None:
        """Wait until `n` bytes are in the buffer. A timeout leaves the buffer as it is: nothing is lost."""
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WireError("the app-server hung up")
            self._buf += chunk

    def _message(self) -> dict | None:
        """The next JSON document, or None for a frame that is not one (a ping, which is answered). A frame is
        taken from the buffer only once all of it has arrived, so a timeout in the middle of one (`next` polls
        with short ones) loses nothing; the part of a message split over frames is kept in between."""
        while True:
            self._fill(2)
            b0, b1 = self._buf[0], self._buf[1]
            n, at = b1 & 0x7F, 2
            if n == 126:
                self._fill(4)
                n, at = struct.unpack(">H", self._buf[2:4])[0], 4
            elif n == 127:
                self._fill(10)
                n, at = struct.unpack(">Q", self._buf[2:10])[0], 10
            masked = bool(b1 & 0x80)
            self._fill(at + (4 if masked else 0) + n)
            mask = self._buf[at:at + 4] if masked else None
            at += 4 if masked else 0
            payload, self._buf = self._buf[at:at + n], self._buf[at + n:]
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            opcode = b0 & 0x0F
            if opcode == 0x9:
                self._frame(0xA, payload)
                return None
            if opcode == 0x8:
                raise WireError("the app-server closed the connection")
            if opcode in (0x0, 0x1, 0x2):
                self._part += payload
                if b0 & 0x80:
                    message, self._part = self._part, b""
                    try:
                        doc = json.loads(message)
                    except ValueError:
                        return None
                    return doc if isinstance(doc, dict) else None
            else:
                return None

    # ---- questions ----

    def _send(self, doc: dict) -> None:
        try:
            self._frame(0x1, json.dumps(doc).encode())
        except OSError as e:
            raise WireError(f"the app-server hung up: {e}") from None

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"method": method} if params is None else {"method": method, "params": params})

    def request(self, method: str, params: dict | None = None) -> dict:
        """The `result` of `method`. WireError when the answer is an error, or none comes in `timeout`."""
        self._next += 1
        mine = self._next
        self._send({"id": mine, "method": method, "params": params or {}})
        deadline = time.monotonic() + self.timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise WireError(f"no answer to {method} in {self.timeout:g} s")
            self.sock.settimeout(left)
            try:
                doc = self._message()
            except socket.timeout:
                raise WireError(f"no answer to {method} in {self.timeout:g} s") from None
            except OSError as e:
                raise WireError(f"the app-server hung up: {e}") from None
            if not doc or doc.get("id") != mine or "method" in doc:
                if doc and "method" in doc and "id" not in doc:
                    self._kept.append(doc)
                continue
            if doc.get("error") is not None:
                err = doc["error"]
                raise WireError(f"{method}: {err.get('message') if isinstance(err, dict) else err}")
            result = doc.get("result")
            return result if isinstance(result, dict) else {}

    def next(self, timeout: float) -> dict | None:
        """The next notification ({"method", "params"}): one kept while an answer was awaited, else the one
        that arrives within `timeout` seconds; None when none does. WireError when the app-server hung up."""
        if self._kept:
            return self._kept.popleft()
        deadline = time.monotonic() + max(timeout, 0)
        while True:
            self.sock.settimeout(max(deadline - time.monotonic(), 0.001))
            try:
                doc = self._message()
            except socket.timeout:
                return None
            except OSError as e:
                raise WireError(f"the app-server hung up: {e}") from None
            if doc and "method" in doc and "id" not in doc:
                return doc
            if time.monotonic() >= deadline:
                return None

    def initialize(self, version: str = "0") -> dict:
        """The handshake every connection starts with. The answer names the Codex folder in use (`codexHome`)."""
        result = self.request("initialize", {
            "clientInfo": {"name": "tares-factory", "title": "Tares Factory", "version": version},
            "capabilities": {"experimentalApi": True}})
        self.notify("initialized")
        return result

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
