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

It also keeps conversations the way a station's needs them (TR-1593), as 0.171.0 was measured to: `thread/start`
gives a new one, idle; `turn/start` puts it in a turn that ends by itself after $FAKE_CODEX_TURN seconds (0.2
when unset) and goes on when the connection that started it leaves; a second `turn/start` into a running turn is
taken as a steer and answers with the running turn's id; `thread/read` says idle, active (with
`waitingOnApproval` under FAKE_CODEX_APPROVAL) or notLoaded, and when it last did anything, fails for the first
$FAKE_CODEX_UNREAD reads after a turn starts (2 when unset), and answers "thread not
loaded" for a conversation it does not know; `turn/interrupt` ends
the turn; a conversation outlives the process in $CODEX_HOME/fake-threads.json, where a new process knows it
as not loaded until `thread/resume`, which fails for one that never had a turn. Each of those requests is
appended to $FAKE_CODEX_LOG, one JSON document per line.

What it took from its start (arguments, folder, the station's variables, its session) is written to
$FAKE_CODEX_RECORD as JSON. Switches, all from the environment:
  FAKE_CODEX_VERSION   the version `--version` prints
  FAKE_CODEX_MCP       names of MCP servers it says it loaded, comma separated. Without `--disable apps` it
                       adds `codex_apps`, the account's connectors, as the real one does
  FAKE_CODEX_HOME      the folder `initialize` names instead of CODEX_HOME (a Codex that ignores CODEX_HOME)
  FAKE_CODEX_NOTIFY    when set, `config/read` shows a notify program
  FAKE_CODEX_MODE      the socket's mode in octal (600 when unset)
  FAKE_CODEX_PLAIN     when set, the socket is made at the path itself, with no link
  FAKE_CODEX_DAEMON    the folder the socket itself is kept in (/tmp/fake-codex-daemon-<uid> when unset)
  FAKE_CODEX_LATE      seconds between the path appearing and the app-server listening on it
  FAKE_CODEX_TURN      seconds a turn takes; `hang` for a turn that only an interrupt ends
  FAKE_CODEX_UNREAD    how many reads of a conversation fail right after a turn starts
  FAKE_CODEX_APPROVAL  when set, a turn stops on an approval nobody answers
  FAKE_CODEX_REFUSE    when set, `turn/start` is refused with this text
  FAKE_CODEX_DEAF      when set, SIGTERM is ignored (only a kill ends it)
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


THREADS = {}            # id -> {"turns": [...], "updatedAt": seconds, "flags": [...]}
LOADED = set()          # the conversations this process has open
LOCK = threading.Lock()


def _store() -> str:
    return os.path.join(os.environ.get("CODEX_HOME") or ".", "fake-threads.json")


def _load() -> None:
    try:
        with open(_store(), encoding="utf-8") as f:
            THREADS.update(json.load(f))
    except (OSError, ValueError):
        pass
    for t in THREADS.values():   # a turn that ran when the last process ended was cut off
        t["unread"] = 0
        for turn in t["turns"]:
            if turn["status"] == "inProgress":
                turn["status"] = "interrupted"


def _save() -> None:
    kept = {k: v for k, v in THREADS.items() if v["turns"]}   # one that never had a turn leaves no file
    tmp = _store() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(kept, f)
    os.replace(tmp, _store())


def _note(method, params) -> None:
    log = os.environ.get("FAKE_CODEX_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"method": method, "params": params, "station": os.environ.get("FACTORY_STATION", ""),
                                "pid": os.getpid()}) + "\n")


def _running(t):
    return next((turn for turn in t["turns"] if turn["status"] == "inProgress"), None)


def _end_turn(thread_id, turn_id, status="completed") -> None:
    with LOCK:
        t = THREADS.get(thread_id)
        for turn in (t or {}).get("turns", []):
            if turn["id"] == turn_id and turn["status"] == "inProgress":
                turn["status"] = status
                t["flags"] = []
                t["updatedAt"] = int(time.time())
                _save()


def _view(thread_id, turns=False) -> dict:
    t = THREADS[thread_id]
    if thread_id not in LOADED:
        status = {"type": "notLoaded"}
    elif _running(t):
        status = {"type": "active", "activeFlags": list(t.get("flags") or [])}
    else:
        status = {"type": "idle"}
    return {"id": thread_id, "status": status, "updatedAt": t["updatedAt"],
            "turns": [{"id": x["id"], "status": x["status"]} for x in t["turns"]] if turns else []}


class Refused(Exception):
    pass


def conversation(method, params):
    """The answer to a conversation request, or None when `method` is not one. Raises Refused with the error."""
    env = os.environ
    if method not in ("thread/start", "thread/read", "thread/resume", "turn/start", "turn/interrupt"):
        return None
    if method != "thread/read":
        _note(method, params)
    with LOCK:
        if method == "thread/start":
            tid = "thr-" + os.urandom(4).hex()
            THREADS[tid] = {"turns": [], "updatedAt": int(time.time()), "flags": []}
            LOADED.add(tid)
            return {"thread": _view(tid)}
        tid = params.get("threadId")
        if tid not in THREADS:
            raise Refused(f"no rollout found for thread id {tid}" if method == "thread/resume" else f"thread not loaded: {tid}")
        t = THREADS[tid]
        if method == "thread/read":
            if t.get("unread", 0) > 0:   # for a moment after a turn starts the conversation cannot be read
                t["unread"] -= 1
                raise Refused("failed to read thread: thread-store internal error")
            if params.get("includeTurns") and not t["turns"]:
                raise Refused("list_turns is not supported yet")
            return {"thread": _view(tid, bool(params.get("includeTurns")))}
        if method == "thread/resume":
            if not t["turns"]:
                raise Refused(f"no rollout found for thread id {tid}")
            LOADED.add(tid)
            return {"thread": _view(tid)}
        if tid not in LOADED:
            raise Refused(f"thread not loaded: {tid}")
        if method == "turn/interrupt":
            turn = _running(t)
            if turn and turn["id"] == params.get("turnId"):
                turn["status"] = "interrupted"
                t["flags"] = []
                t["updatedAt"] = int(time.time())
                _save()
            return {}
        if env.get("FAKE_CODEX_REFUSE"):
            raise Refused(env["FAKE_CODEX_REFUSE"])
        running = _running(t)
        if running:   # a steer of the running turn, not a new one
            return {"turn": {"id": running["id"], "status": "inProgress"}}
        turn = {"id": "turn-" + os.urandom(4).hex(), "status": "inProgress",
                "text": "".join(i.get("text", "") for i in params.get("input") or [])}
        t["turns"].append(turn)
        t["updatedAt"] = int(time.time())
        t["flags"] = ["waitingOnApproval"] if env.get("FAKE_CODEX_APPROVAL") else []
        t["unread"] = int(env.get("FAKE_CODEX_UNREAD") or 2)
        _save()
        took = env.get("FAKE_CODEX_TURN") or "0.2"
        if took != "hang" and not env.get("FAKE_CODEX_APPROVAL"):
            timer = threading.Timer(float(took), _end_turn, args=(tid, turn["id"]))
            timer.daemon = True
            timer.start()
        return {"turn": {"id": turn["id"], "status": "inProgress"}}


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
            try:
                result = conversation(msg.get("method"), msg.get("params") or {})
            except Refused as e:
                send_text(conn, {"id": msg["id"], "error": {"code": -32600, "message": str(e)}})
                continue
            if result is None:
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
        folder = os.environ.get("FAKE_CODEX_DAEMON") or f"/tmp/fake-codex-daemon-{os.getuid()}"
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
    time.sleep(float(os.environ.get("FAKE_CODEX_LATE") or 0))
    server.listen(8)

    def end(*_a):
        for left in {path, real}:
            try:
                os.unlink(left)
            except OSError:
                pass
        os._exit(0)
    signal.signal(signal.SIGTERM, signal.SIG_IGN if os.environ.get("FAKE_CODEX_DEAF") else end)
    _load()
    while True:
        conn, _ = server.accept()
        threading.Thread(target=serve, args=(conn, argv), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
