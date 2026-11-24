#!/usr/bin/env python3
"""A stand-in for the `codex` program, for the Codex launch tests (tests/test_crew_launch_codex.py, TR-1592).

    fake_codex.py --version
    fake_codex.py app-server --listen unix://PATH [--disable FEATURE ...]

`--version` prints `codex-cli $FAKE_CODEX_VERSION` (0.171.0 when unset). `app-server` opens the socket as
Codex 0.171.0 does on macOS: the socket itself, owner-only, in a folder of its own under /tmp, and the path it
was given made a link to it. It answers the three questions a station's start asks, as that version answers
them (TR-1576): `initialize` (with `codexHome`, the CODEX_HOME it runs on), `mcpServerStatus/list` and
`config/read`. On the socket each JSON document is one WebSocket text frame. SIGTERM removes the link and the
socket and exits 0; a kill leaves both behind, and the next start replaces them, as with the real one.

What it took from its start (arguments, folder, the station's variables, its session) is written to
$FAKE_CODEX_RECORD as JSON. Switches, all from the environment:
  FAKE_CODEX_VERSION   the version `--version` prints
  FAKE_CODEX_MCP       names of MCP servers it says it loaded, comma separated. Without `--disable apps` it
                       adds `codex_apps`, the account's connectors, as the real one does
  FAKE_CODEX_HOME      the folder `initialize` names instead of CODEX_HOME (a Codex that ignores CODEX_HOME)
  FAKE_CODEX_NOTIFY    when set, `config/read` shows a notify program
  FAKE_CODEX_MODE      the socket's mode in octal (600 when unset)
  FAKE_CODEX_PLAIN     when set, the socket is made at the path itself, with no link
  FAKE_CODEX_DIE       when set, the app-server prints a line and exits 3 at once
  FAKE_CODEX_SILENT    when set, the app-server never opens its socket

Standard library only.
"""
import base64
import hashlib
import json
import os
import signal
import socket
import struct
import sys
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def take(conn, n):
    out = b""
    while len(out) < n:
        chunk = conn.recv(n - len(out))
        if not chunk:
            raise EOFError
        out += chunk
    return out


def read_frame(conn):
    b0, b1 = take(conn, 2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", take(conn, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", take(conn, 8))[0]
    mask = take(conn, 4) if b1 & 0x80 else None
    payload = take(conn, n)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return b0 & 0x0F, payload


def send_text(conn, doc):
    data = json.dumps(doc).encode()
    n = len(data)
    head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536
                            else bytes([127]) + struct.pack(">Q", n))
    conn.sendall(head + data)


def answer(method, args):
    env = os.environ
    if method == "initialize":
        return {"userAgent": "fake-codex", "codexHome": env.get("FAKE_CODEX_HOME") or env.get("CODEX_HOME", ""),
                "platformOs": "test"}
    if method == "mcpServerStatus/list":
        names = [n for n in env.get("FAKE_CODEX_MCP", "").split(",") if n]
        off = {args[i + 1] for i, a in enumerate(args[:-1]) if a == "--disable"}
        if "apps" not in off:
            names.append("codex_apps")
        return {"data": [{"name": n, "tools": {}} for n in names], "nextCursor": None}
    if method == "config/read":
        return {"config": {"notify": ["/person/notify-program"] if env.get("FAKE_CODEX_NOTIFY") else None},
                "origins": {}}
    return None


def serve(conn, args):
    try:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(4096)
            if not chunk:
                return
            head += chunk
        key = [line.split(b":", 1)[1].strip() for line in head.split(b"\r\n") if line.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        while True:
            opcode, payload = read_frame(conn)
            if opcode == 0x8:
                return
            if opcode != 0x1:
                continue
            msg = json.loads(payload)
            if "id" not in msg:
                continue
            result = answer(msg.get("method"), args)
            if result is None:
                send_text(conn, {"id": msg["id"], "error": {"code": -32601, "message": "no such method"}})
            else:
                send_text(conn, {"id": msg["id"], "result": result})
    except (EOFError, OSError, ValueError, IndexError):
        return
    finally:
        conn.close()


def main(argv):
    if argv[:1] == ["--version"]:
        print("codex-cli " + os.environ.get("FAKE_CODEX_VERSION", "0.171.0"))
        return 0
    if argv[:1] != ["app-server"] or "--listen" not in argv:
        print("fake codex: only --version and app-server --listen are here", file=sys.stderr)
        return 2
    listen = argv[argv.index("--listen") + 1]
    path = listen.removeprefix("unix://")
    record = os.environ.get("FAKE_CODEX_RECORD")
    if record:
        keep = {k: v for k, v in os.environ.items()
                if k.startswith(("FACTORY_", "TARES", "CODEX_HOME", "NO_COLOR")) or k in ("PATH", "HOME")}
        with open(record, "w", encoding="utf-8") as f:
            json.dump({"argv": argv, "cwd": os.getcwd(), "env": keep, "pid": os.getpid(), "sid": os.getsid(0),
                       "stdin_tty": sys.stdin.isatty()}, f)
    if os.environ.get("FAKE_CODEX_DIE"):
        print("fake codex: told to end at once", file=sys.stderr)
        return 3
    if os.environ.get("FAKE_CODEX_SILENT"):
        time.sleep(600)
        return 0
    real = path
    if not os.environ.get("FAKE_CODEX_PLAIN"):
        folder = f"/tmp/fake-codex-daemon-{os.getuid()}"
        os.makedirs(folder, mode=0o700, exist_ok=True)
        real = os.path.join(folder, hashlib.sha256(path.encode()).hexdigest())
    for stale in {real, path}:   # what a killed one left
        if os.path.lexists(stale):
            os.unlink(stale)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        server.bind(real)
    finally:
        os.umask(old)
    os.chmod(real, int(os.environ.get("FAKE_CODEX_MODE", "600"), 8))
    if real != path:
        os.symlink(real, path)
    server.listen(8)

    def end(*_a):
        for left in {path, real}:
            try:
                os.unlink(left)
            except OSError:
                pass
        os._exit(0)
    signal.signal(signal.SIGTERM, end)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=serve, args=(conn, argv), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
