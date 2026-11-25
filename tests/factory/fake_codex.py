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

It runs a conversation's hooks and keeps what a turn did (TR-1594), as 0.171.0 was measured to. A conversation
keeps the `config` and `cwd` it was started or joined with. With `bypass_hook_trust` in that config, a turn
runs the `UserPromptSubmit` hooks first, then tells the connections that started or joined the conversation
of its first item (`item/started`, a `userMessage`); without it no hook runs and nothing is said about that.
The "model" then runs $FAKE_CODEX_COMMANDS, a JSON list: a string is a shell command, {"mcp": "server__tool"}
an MCP call. Each goes to the `PreToolUse` hooks first (the payload Codex gives: `tool_name`, `tool_input`,
`tool_use_id`, `turn_id`, `cwd`); a deny leaves no item and nothing runs, an allow with `updatedInput` runs
the command it gives. What ran is kept as the turn's items, listed by `thread/turns/list` once the command has
ended.
A connection that only reads is told nothing of a turn. `hooks/list` and `config/read` answer for a folder
as the real one does: a folder nobody marked is recorded as trusted in $CODEX_HOME/config.toml when a
conversation starts in it, and from then on a repository that brings its own setup ($FAKE_CODEX_PROJECT)
shows its hook and its MCP server; a folder marked `untrusted`, there or in the conversation's start, shows
neither and is not recorded.

It connects a conversation's MCP servers (TR-1596), as 0.171.0 was measured to. A server the conversation's
`config.mcp_servers` gives with a `url` is reached over HTTP when the conversation is started, or joined by an
app-server that did not have it open: the `http_headers_helper` is run through a shell with a short
environment of its own (HOME, PATH and a few more, none of the station's variables) and what it prints is
taken as headers; each header of `env_http_headers` is added from the app-server's own environment, and left
off when its variable is empty or unset; then `initialize` is posted to the url. A server that does not
answer 200 is `failed`, and when it is marked `required` the conversation is not started or joined at all
("required MCP servers failed to initialize: ..."). `mcpServerStatus/list` with a `threadId` gives that
conversation's servers with their `runtimeStatus`; `mcpServer/tool/call` posts `tools/call` on the same
connection, with the same headers, and gives the server's answer.

It asks, as 0.171.0 was measured to (TR-1593). An entry of $FAKE_CODEX_COMMANDS that is {"ask": ...} is a call
the turn stops on, after the `PreToolUse` hooks ran: {"ask": "command", "command": "..."} (a command the
sandbox does not allow), {"ask": "file", "path": "..."} (a change outside the workspace; the request names
only the call), {"ask": "question", "questions": [...]} or {"ask": "other"} (a request of another kind). The
conversation then reads active with `waitingOnApproval`, no item is listed for the call, and the request is
put to every connection that started or joined the conversation, and again to each that joins later: the same
turn, item and start time, with an id that is that connection's own (each connection's first is 0). A
connection that only reads is asked nothing. The first answer counts: "accept" runs the command (a file
change writes "changed" to the path), anything else does not; every connection of the conversation is told
`serverRequest/resolved`, and the turn goes on by itself. An answer to a request that is no longer asked is
dropped.

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
  FAKE_CODEX_FAIL      when set, a turn is taken and then fails with this text before the model does anything
  FAKE_CODEX_COMMANDS  what the model runs in each turn (see above); nothing when unset
  FAKE_CODEX_THINK     seconds the model takes before each command (0.3 when unset)
  FAKE_CODEX_NOHOOKS   when set, no hook runs whatever the conversation's config says (trust is ignored)
  FAKE_CODEX_NOPRE     when set, the `PreToolUse` hooks do not run (the others do)
  FAKE_CODEX_DENY_RUNS when set, a command the hook denied runs all the same
  FAKE_CODEX_KEEP_INPUT  when set, the command runs as the model gave it, not as the hook changed it
  FAKE_CODEX_PROJECT   when set, the repository brings a hook and an MCP server of its own (see above);
                       `force` shows them even in a folder marked untrusted
  FAKE_CODEX_DEAF      when set, SIGTERM is ignored (only a kill ends it)
  FAKE_CODEX_DIE       when set, the app-server prints a line and exits 3 at once
  FAKE_CODEX_SILENT    when set, the app-server never opens its socket

Standard library only.
"""
import base64
import hashlib
import json
import os
import shlex
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# What a headers helper is run with (0.171.0, macOS): none of the station's own variables.
HELPER_ENV = ("HOME", "LC_CTYPE", "LOGNAME", "PATH", "SHELL", "TMPDIR", "USER")
RUST = "Send message error Transport [fake_rmcp::Worker<fake_rmcp::StreamableHttp<fake_rmcp::Adapter>>] error: "


class McpDown(Exception):
    pass


def _mcp_post(server, method, params):
    """One JSON-RPC request to an MCP server over HTTP; its `result`. McpDown with Codex's words otherwise."""
    import urllib.error
    import urllib.request
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(server["url"], data=body, method="POST",
                                 headers={"Content-Type": "application/json", **server["headers"]})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode() or "{}").get("result") or {}
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace").strip()[:80]
        why = f"unexpected server response: HTTP {e.code}: {text}"
        raise McpDown(f"{RUST}{why}, when send {method} request: {RUST}{why}, when send {method} request") from None
    except (OSError, ValueError) as e:
        raise McpDown(f"{RUST}Client error: HTTP request failed: http/request failed: {e}") from None


def mcp_connect(config):
    """({name: connection}, [what failed among the required ones]) for a conversation's `config`."""
    out, failed = {}, []
    for name, c in ((config or {}).get("mcp_servers") or {}).items():
        if not isinstance(c, dict) or not c.get("url"):
            continue
        server = {"url": c["url"], "headers": {}, "status": "connected", "error": None}
        try:
            if c.get("http_headers_helper"):
                env = {k: os.environ[k] for k in HELPER_ENV if k in os.environ}
                done = subprocess.run(["/bin/sh", "-c", c["http_headers_helper"]], env=env, stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, timeout=30)
                if done.returncode != 0:
                    raise McpDown(f"{RUST}Client error: HTTP request failed: MCP HTTP headers helper exited with "
                                  f"status {done.returncode}")
                server["headers"].update({str(k): str(v) for k, v in json.loads(done.stdout or "{}").items()})
            for header, var in (c.get("env_http_headers") or {}).items():
                if os.environ.get(var):
                    server["headers"][header] = os.environ[var]
            _mcp_post(server, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                             "clientInfo": {"name": "fake-codex", "version": "0"}})
        except (McpDown, OSError, ValueError, subprocess.SubprocessError) as e:
            server.update(status="failed", error=f"handshaking with MCP server failed: {e}")
            if c.get("required"):
                failed.append(f"{name}: {server['error']}")
        out[name] = server
    return out, failed


def _required(failed, what):
    if failed:
        raise Refused(f"error {what} thread: Fatal error: Failed to initialize session: required MCP servers failed "
                      f"to initialize: {'; '.join(failed)}")


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


SEND = threading.Lock()   # one frame at a time: a turn's thread and a connection's own write to the same socket


def send_text(conn, doc):
    data = json.dumps(doc).encode()
    n = len(data)
    head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536
                            else bytes([127]) + struct.pack(">Q", n))
    with SEND:
        conn.sendall(head + data)


THREADS = {}            # id -> {"turns": [...], "updatedAt": seconds, "flags": [...], "config": {...}, "cwd": str}
LOADED = set()          # the conversations this process has open
MCP = {}                # id -> {server name: {url, headers, status, error}}: its MCP connections
PENDING = {}            # id -> {"method", "params", "answered": Event, "result", "ids": {connection: request id}}
ASK_IDS = {}            # connection -> the id its next request of the server's own gets
JOINED = {}             # id -> the connections that started or joined it: they are told of its turns
LOCK = threading.Lock()


def _tell(thread_id, method, params) -> None:
    for conn in list(JOINED.get(thread_id) or []):
        try:
            send_text(conn, {"method": method, "params": {"threadId": thread_id, **params}})
        except OSError:
            JOINED[thread_id].discard(conn)


def _home_config() -> str:
    return os.path.join(os.environ.get("CODEX_HOME") or ".", "config.toml")


def _marks() -> dict:
    """folder -> trust level, from the Codex folder's config.toml (only the `projects` tables are read)."""
    found, folder = {}, None
    try:
        with open(_home_config(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("[projects.") and line.endswith("]"):
                    folder = json.loads(line[len("[projects."):-1])
                elif folder and line.startswith("trust_level"):
                    found[folder] = json.loads(line.split("=", 1)[1].strip())
                elif line.startswith("["):
                    folder = None
    except (OSError, ValueError):
        pass
    return found


def _trusted(cwd) -> bool:
    if os.environ.get("FAKE_CODEX_PROJECT") == "force":
        return True
    marks = _marks()
    return any(marks.get(f) == "trusted" for f in (cwd, os.path.realpath(cwd or ".")))


def _settle_trust(params) -> None:
    """A conversation starts in a folder: one nobody marked is recorded as trusted, as the real one does."""
    cwd = params.get("cwd")
    if not cwd:
        return
    names = {cwd, os.path.realpath(cwd)}
    session = (params.get("config") or {}).get("projects") or {}
    if any(f in session for f in names) or any(f in _marks() for f in names):
        return
    with open(_home_config(), "a", encoding="utf-8") as f:
        f.write(f"[projects.{json.dumps(os.path.realpath(cwd))}]\ntrust_level = \"trusted\"\n\n")


def _hook(entry, payload, cwd):
    """Run one hook command with the payload on stdin: (exit code, what it printed as JSON or {})."""
    try:
        r = subprocess.run(entry.get("command") or "true", shell=True, input=json.dumps(payload).encode(),
                           capture_output=True, cwd=cwd or None, timeout=float(entry.get("timeout") or 5))
    except (OSError, subprocess.SubprocessError):
        return 1, {}
    try:
        doc = json.loads(r.stdout or b"{}")
    except ValueError:
        doc = {}
    return r.returncode, doc if isinstance(doc, dict) else {}


def _hooks(t, event) -> list:
    env, config = os.environ, t.get("config") or {}
    if env.get("FAKE_CODEX_NOHOOKS") or not config.get("bypass_hook_trust"):
        return []
    if event == "PreToolUse" and env.get("FAKE_CODEX_NOPRE"):
        return []
    return [h for group in (config.get("hooks") or {}).get(event) or [] for h in group.get("hooks") or []]


def _still(thread_id, turn_id) -> bool:
    with LOCK:
        return any(x["id"] == turn_id and x["status"] == "inProgress" for x in THREADS[thread_id]["turns"])


def _run_turn(thread_id, turn_id, prompt) -> None:
    """What a turn does after `turn/start` was answered (see the module's note)."""
    env, t = os.environ, THREADS[thread_id]
    cwd = t.get("cwd")
    base = {"session_id": thread_id, "turn_id": turn_id, "cwd": cwd, "model": "fake", "permission_mode": "default"}
    for h in _hooks(t, "UserPromptSubmit"):
        _hook(h, {**base, "hook_event_name": "UserPromptSubmit", "prompt": prompt}, cwd)
    if env.get("FAKE_CODEX_FAIL"):   # as a model the account cannot use: the turn fails, the conversation is at rest
        with LOCK:
            for turn in t["turns"]:
                if turn["id"] == turn_id:
                    turn["error"] = {"message": env["FAKE_CODEX_FAIL"], "codexErrorInfo": "modelNotFound"}
        _end_turn(thread_id, turn_id, "failed")
        return
    _tell(thread_id, "item/started", {"turnId": turn_id, "item": {"type": "userMessage", "id": "msg-" + os.urandom(4).hex()}})
    try:
        commands = json.loads(env.get("FAKE_CODEX_COMMANDS") or "[]")
    except ValueError:
        commands = []
    for c in commands:
        time.sleep(float(env.get("FAKE_CODEX_THINK") or 0.3))
        if not _still(thread_id, turn_id):
            return
        call = "exec-" + os.urandom(6).hex()
        mcp = c.get("mcp") if isinstance(c, dict) else None
        ask = c.get("ask") if isinstance(c, dict) else None
        asked = "" if mcp or ask in ("file", "question", "other") else str(c.get("command") if ask else c)
        tool = "mcp__" + mcp if mcp else "apply_patch" if ask == "file" else "Bash"
        denied, runs = False, asked
        for h in _hooks(t, "PreToolUse"):
            code, said = _hook(h, {**base, "hook_event_name": "PreToolUse", "tool_name": tool, "tool_use_id": call,
                                   "tool_input": {} if mcp else {"command": asked}}, cwd)
            out = said.get("hookSpecificOutput") if isinstance(said.get("hookSpecificOutput"), dict) else {}
            if code == 2 or out.get("permissionDecision") == "deny":
                denied = True
            elif isinstance(out.get("updatedInput"), dict) and isinstance(out["updatedInput"].get("command"), str):
                runs = out["updatedInput"]["command"]
        if denied and not env.get("FAKE_CODEX_DENY_RUNS"):
            continue
        if env.get("FAKE_CODEX_KEEP_INPUT"):
            runs = asked
        if ask:
            result = _ask(thread_id, turn_id, call, ask, c, runs, cwd)
            if result is None or not _still(thread_id, turn_id):
                return   # the turn was ended while it waited
            if ask in ("question", "other") or result.get("decision") not in ("accept", "acceptForSession"):
                continue
            if ask == "file":
                with open(c["path"], "w", encoding="utf-8") as f:
                    f.write("changed\n")
                continue
        if mcp:
            server, _, name = mcp.partition("__")
            item = {"type": "mcpToolCall", "id": call, "server": server, "tool": name, "status": "completed"}
        else:
            _tell(thread_id, "item/started", {"turnId": turn_id, "item": {"type": "commandExecution", "id": call}})
            subprocess.run(runs, shell=True, cwd=cwd or None, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            item = {"type": "commandExecution", "id": call, "command": "/bin/zsh -lc " + shlex.quote(runs),
                    "commandActions": [{"type": "unknown", "command": runs}], "source": "unifiedExecStartup",
                    "status": "completed"}
        with LOCK:
            for turn in t["turns"]:
                if turn["id"] == turn_id:
                    turn.setdefault("items", []).append(item)
            _save()
    took = env.get("FAKE_CODEX_TURN") or "0.2"
    if took == "hang" or env.get("FAKE_CODEX_APPROVAL"):
        return
    time.sleep(float(took))
    if _still(thread_id, turn_id):
        _end_turn(thread_id, turn_id)
        _tell(thread_id, "turn/completed", {"turn": {"id": turn_id, "status": "completed"}})


def _put(conn, thread_id) -> None:
    """Put the conversation's waiting request to one connection, under an id of that connection's own."""
    with LOCK:
        pending = PENDING.get(thread_id)
        if pending is None or pending["answered"].is_set():
            return
        number = ASK_IDS.get(conn, 0)
        ASK_IDS[conn] = number + 1
        pending["ids"][conn] = number
    try:
        send_text(conn, {"id": number, "method": pending["method"], "params": pending["params"]})
    except OSError:
        pass


def _ask(thread_id, turn_id, call, kind, c, command, cwd):
    """Stop the turn on a request and wait for its answer; the answer's result, or None when the turn ended."""
    method = {"command": "item/commandExecution/requestApproval", "file": "item/fileChange/requestApproval",
              "question": "item/tool/requestUserInput"}.get(kind, "item/permissions/requestApproval")
    params = {"threadId": thread_id, "turnId": turn_id, "itemId": call, "startedAtMs": int(time.time() * 1000)}
    if kind == "command":
        params.update(command="/bin/zsh -lc " + shlex.quote(command), cwd=cwd, kind="command", reason=c.get("reason"))
    elif kind == "file":
        params.update(reason=c.get("reason"), grantRoot=None)
    elif kind == "question":
        params.update(questions=c.get("questions") or [], isBlocking=True)
    pending = {"method": method, "params": params, "answered": threading.Event(), "result": None, "ids": {}}
    with LOCK:
        PENDING[thread_id] = pending
        THREADS[thread_id]["flags"] = ["waitingOnApproval"]
        joined = list(JOINED.get(thread_id, ()))
    for conn in joined:
        _put(conn, thread_id)
    while not pending["answered"].wait(0.1):
        if not _still(thread_id, turn_id):
            with LOCK:
                PENDING.pop(thread_id, None)
            return None
    with LOCK:
        PENDING.pop(thread_id, None)
        THREADS[thread_id]["flags"] = []
    return pending["result"] if isinstance(pending["result"], dict) else {}


def _answered(conn, msg) -> None:
    """A connection's answer to a request of the server's own. The first answer to a waiting request counts."""
    with LOCK:
        found = [(tid, p) for tid, p in PENDING.items() if p["ids"].get(conn) == msg.get("id") and not p["answered"].is_set()]
        if not found:
            return
        thread_id, pending = found[0]
        pending["result"] = msg.get("result")
        pending["answered"].set()
    _note("answer", {"threadId": thread_id, "method": pending["method"], "result": msg.get("result")})
    _tell(thread_id, "serverRequest/resolved", {"threadId": thread_id, "requestId": msg.get("id")})


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
            "turns": [{"id": x["id"], "status": x["status"], "error": x.get("error")} for x in t["turns"]] if turns else []}


class Refused(Exception):
    pass


def conversation(method, params, conn=None):
    """The answer to a conversation request, or None when `method` is not one. Raises Refused with the error."""
    env = os.environ
    if method not in ("thread/start", "thread/read", "thread/resume", "thread/turns/list", "turn/start",
                      "turn/interrupt"):
        return None
    if method not in ("thread/read", "thread/turns/list"):
        _note(method, params)
    with LOCK:
        if method == "thread/start":
            servers, failed = mcp_connect(params.get("config"))
            _required(failed, "creating")
            tid = "thr-" + os.urandom(4).hex()
            THREADS[tid] = {"turns": [], "updatedAt": int(time.time()), "flags": [],
                            "config": params.get("config") or {}, "cwd": params.get("cwd")}
            MCP[tid] = servers
            LOADED.add(tid)
            JOINED.setdefault(tid, set()).add(conn)
            _settle_trust(params)
            return {"thread": _view(tid)}
        tid = params.get("threadId")
        if tid not in THREADS:
            raise Refused(f"no rollout found for thread id {tid}" if method == "thread/resume" else f"thread not loaded: {tid}")
        t = THREADS[tid]
        if method == "thread/turns/list":
            full = params.get("itemsView") == "full"
            turns = [{"id": x["id"], "status": x["status"], "itemsView": "full" if full else "summary",
                      "items": list(x.get("items") or []) if full else []}
                     for x in reversed(t["turns"])]
            return {"data": turns[:int(params.get("limit") or 50)], "nextCursor": None}
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
            if tid not in LOADED:   # one it has open keeps the connections it has
                servers, failed = mcp_connect(params.get("config") if "config" in params else t.get("config"))
                _required(failed, "resuming")
                MCP[tid] = servers
            LOADED.add(tid)
            JOINED.setdefault(tid, set()).add(conn)
            if "config" in params:
                t["config"] = params.get("config") or {}
            t["cwd"] = params.get("cwd") or t.get("cwd")
            _settle_trust(params)
            if tid in PENDING:   # whoever joins is asked again, once the join is answered
                threading.Timer(0.05, _put, (conn, tid)).start()
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
        # after the answer has left: the hooks, the first item, what the model runs, the end
        timer = threading.Timer(0.03, _run_turn, args=(tid, turn["id"], turn["text"]))
        timer.daemon = True
        timer.start()
        return {"turn": {"id": turn["id"], "status": "inProgress"}}


def answer(method, args, params):
    env = os.environ
    if method == "initialize":
        return {"userAgent": "fake-codex", "codexHome": env.get("FAKE_CODEX_HOME") or env.get("CODEX_HOME", ""),
                "platformOs": "test"}
    if method == "mcpServerStatus/list" and params.get("threadId"):
        rows = [{"name": n, "tools": {}, "authStatus": "unknown", "runtimeStatus": s["status"]}
                for n, s in MCP.get(params["threadId"], {}).items() if params.get("serverName") in (None, n)]
        return {"data": rows, "nextCursor": None}
    if method == "mcpServer/tool/call":
        server = MCP.get(params.get("threadId"), {}).get(params.get("server"))
        if server is None:
            raise Refused(f"unknown MCP server '{params.get('server')}'")
        if server["status"] != "connected":
            raise Refused(f"failed to get client: MCP startup failed: {server['error']}")
        try:
            got = _mcp_post(server, "tools/call", {"name": params.get("tool"), "arguments": params.get("arguments") or {}})
        except McpDown as e:
            raise Refused(f"tool call failed: {e}") from None
        _note(method, {k: params.get(k) for k in ("server", "tool", "arguments", "threadId")})
        return {"content": got.get("content") or [], "isError": got.get("isError")}
    if method == "mcpServerStatus/list":
        names = [n for n in env.get("FAKE_CODEX_MCP", "").split(",") if n]
        off = {args[i + 1] for i, a in enumerate(args[:-1]) if a == "--disable"}
        if "apps" not in off:
            names.append("codex_apps")
        return {"data": [{"name": n, "tools": {}} for n in names], "nextCursor": None}
    if method == "config/read":
        brings = bool(env.get("FAKE_CODEX_PROJECT")) and _trusted(params.get("cwd"))
        return {"config": {"notify": ["/person/notify-program"] if env.get("FAKE_CODEX_NOTIFY") else None,
                           "mcp_servers": {"repo_server": {"command": "x"}} if brings else {}},
                "origins": {}}
    if method == "hooks/list":
        data = []
        for cwd in params.get("cwds") or []:
            brings = bool(env.get("FAKE_CODEX_PROJECT")) and _trusted(cwd)
            data.append({"cwd": cwd, "hooks": [{"eventName": "preToolUse", "source": "project", "trustStatus": "untrusted",
                                                "sourcePath": os.path.join(cwd, ".codex", "config.toml")}] if brings else []})
        return {"data": data}
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
            if "method" not in msg:   # an answer to a request of the server's own
                _answered(conn, msg)
                continue
            try:
                result = conversation(msg.get("method"), msg.get("params") or {}, conn)
            except Refused as e:
                send_text(conn, {"id": msg["id"], "error": {"code": -32600, "message": str(e)}})
                continue
            if result is None:
                try:
                    result = answer(msg.get("method"), args, msg.get("params") or {})
                except Refused as e:
                    send_text(conn, {"id": msg["id"], "error": {"code": -32603, "message": str(e)}})
                    continue
            if result is None:
                send_text(conn, {"id": msg["id"], "error": {"code": -32601, "message": "no such method"}})
            else:
                send_text(conn, {"id": msg["id"], "result": result})
    except (EOFError, OSError, ValueError, IndexError):
        return
    finally:
        for joined in JOINED.values():
            joined.discard(conn)
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
