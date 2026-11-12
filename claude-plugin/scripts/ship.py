#!/usr/bin/env python3
"""Tares plugin shipper — runs as a Claude Code hook and streams this session into Tares.

Every hook receives the session's `transcript_path` (the JSONL Claude Code writes) on stdin. We read
the *new* lines since the last run (tracked by a byte offset in the plugin's persistent data dir) and
POST them as NDJSON to Tares's claude_code source at {tares_url}/ingest/claude_code. The daemon
maps each line into the data plane (same mapping as the local file tail).

The source is created automatically on Tares the first time the plugin runs (and re-created if it's
missing), so installing the plugin is the only setup step — no need to provision a source by hand.

Session flows (the challenger workflow): when Claude calls the `set_session_flow` MCP tool, that
call is a tool_use line in the transcript. The shipper is the one component that sees the transcript
together with the session id, so it is the bridge: it writes a per-session marker file the local
challenger hooks gate on, stamps `flow` on every line it ships from then on, and writes synthetic
`session_flow` / `session_end` lines so Tares can tell a challenger session and its end apart.
Tares is never asked whether a session is marked; the decision travels through the transcript.

Idempotent: the offset only advances after a successful POST. Best-effort: never raises into the
session. Configured entirely via plugin userConfig env vars.
"""
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Claude Code's own bookkeeping lines in a transcript: no conversation in them, so they are not
# shipped (about half of a session's lines otherwise)
BOOKKEEPING = {"attachment", "queue-operation", "mode", "permission-mode", "atis-latch",
               "ai-title", "last-prompt", "file-history-snapshot",
               # Claude Code's record of each API request (2.1.3xx): an api-request-blob carries a
               # copy of the previous assistant message, every tool call in it again, and no
               # timestamp, so Tares would list each call a second time at the moment it arrived
               "api-request", "api-request-blob", "api-request-shape"}

FLOW_TOOL = "set_session_flow"   # the tares MCP tool; Claude Code prefixes the server name
                                  # (mcp__tares__..., or mcp__plugin_tares_tares__... from a plugin)
CHALLENGER = "challenger"


def _truthy(v: str) -> bool:
    return str(v).strip().lower() not in ("0", "false", "off", "no", "")


def _opt(name: str, default: str = "") -> str:
    """userConfig option from the hook env. Claude Code injects CLAUDE_PLUGIN_OPTION_<KEY> with
    the key UPPERCASED (user_config.access_token -> CLAUDE_PLUGIN_OPTION_ACCESS_TOKEN); check the
    verbatim casing too for robustness across versions."""
    return (os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name.upper()}")
            or os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name}")
            or default)


LOCAL = "http://127.0.0.1:8787"


def _saved_login():
    """`tares login`'s saved workspace: {cell_url, key}, or None."""
    home = os.environ.get("TARES_HOME") or os.path.expanduser("~/.tares")
    try:
        with open(os.path.join(home, "login.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("cell_url") or not data.get("key"):
        return None
    return {"cell_url": str(data["cell_url"]).rstrip("/"), "key": str(data["key"])}


def _file_token() -> str:
    path = (os.environ.get("TARES_FACTORY_TOKEN_FILE") or "").strip()
    if not path:
        return ""
    try:
        with open(os.path.expanduser(path)) as f:
            return f.read().strip()
    except OSError:
        return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends the Authorization header on a redirect, to any host. Tares never redirects
    these calls, so a redirect is refused (it surfaces as an HTTPError) rather than followed."""
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _post(url: str, data: bytes, headers: dict, timeout: float = 5):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    return _opener.open(req, timeout=timeout)


def _get(url: str, headers: dict, timeout: float = 3):
    req = urllib.request.Request(url, headers=headers, method="GET")
    return _opener.open(req, timeout=timeout)


def _auth_only(headers: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if "Authorization" in headers:
        h["Authorization"] = headers["Authorization"]
    return h


# ── shared with challenger.py ────────────────────────────────────────────────

def config() -> dict:
    """Where Tares is, how to talk to it, and where this plugin keeps its state."""
    # A tares-factory station is told its crew's Tares (TARES_FACTORY_URL) and where its token is
    # kept (TARES_FACTORY_TOKEN_FILE); both win over the plugin options, whose URL defaults to
    # 127.0.0.1:8787.
    # Then a tares_url set by hand, then TARESD_URL, then the workspace `tares login` saved (TR-492),
    # then the local daemon: the same order `tares` itself uses. The saved key only ever goes to
    # its own workspace, and never when an access_token was set by hand.
    opt_url = (_opt("tares_url") or "").rstrip("/")
    token = (_file_token() or _opt("access_token") or "").strip()
    login = None if token else _saved_login()     # a token set by hand keeps its own address
    base = (os.environ.get("TARES_FACTORY_URL") or (opt_url if opt_url != LOCAL else "")
            or os.environ.get("TARESD_URL") or (login["cell_url"] if login else "")
            or LOCAL).rstrip("/")
    if login and not os.environ.get("TARES_FACTORY_URL") and base == login["cell_url"]:
        token = login["key"]
    headers = {"Content-Type": "application/x-ndjson"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data_dir = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.expanduser("~/.tares-plugin")
    os.makedirs(data_dir, exist_ok=True)
    return {"base": base, "headers": headers, "data_dir": data_dir}


def flow_marker(data_dir: str, session_id: str) -> str:
    d = os.path.join(data_dir, "sessions")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, hashlib.sha1(str(session_id).encode()).hexdigest()[:16] + ".flow")


def read_flow(data_dir: str, session_id: str) -> str:
    """The flow this session is marked with ("" when unmarked)."""
    try:
        return open(flow_marker(data_dir, session_id)).read().strip()
    except OSError:
        return ""


def write_flow(data_dir: str, session_id: str, flow: str) -> None:
    path = flow_marker(data_dir, session_id)
    if flow:
        with open(path, "w") as f:
            f.write(flow)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def git_branch(cwd: str) -> str:
    try:
        out = subprocess.run(["git", "-C", cwd or ".", "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def synthetic_line(hook: dict, typ: str, flow: str, **extra) -> dict:
    """A transcript-shaped line the shipper (or a challenger hook) writes itself, keyed like the
    real lines so it lands on the same session timeline."""
    cwd = hook.get("cwd") or os.getcwd()
    o = {"type": typ, "sessionId": hook.get("session_id"), "cwd": cwd, "timestamp": now_iso(),
         "version": "tares-plugin"}
    branch = git_branch(cwd)
    if branch:
        o["gitBranch"] = branch
    if flow:
        o["flow"] = flow
    o.update(extra)
    return o


def ship_lines(cfg: dict, objs: list) -> bool:
    """POST transcript-shaped objects to the claude_code source. Best effort."""
    if not objs:
        return True
    body = ("\n".join(json.dumps(o, separators=(",", ":")) for o in objs) + "\n").encode()
    return _ship_body(cfg, body)


# Why the last POST failed, when the size of the request may be the reason: "refused" (413, this
# request will never be taken) or "slow" (it outlasted the timeout). Empty for any other outcome.
_size_failure = ""


def _timed_out(e: BaseException) -> bool:
    reason = getattr(e, "reason", e)
    return isinstance(e, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout))


def _ship_body(cfg: dict, body: bytes) -> bool:
    global _size_failure
    _size_failure = ""
    base, headers = cfg["base"], cfg["headers"]
    marker = os.path.join(cfg["data_dir"], "ensured-" + hashlib.sha1(base.encode()).hexdigest()[:12])
    try:
        _post(base + "/ingest/claude_code", body, headers)
        return True
    except urllib.error.HTTPError as e:
        if e.code == 413:
            _size_failure = "refused"
        if e.code == 404:          # source went missing — recreate and retry once
            try:
                os.remove(marker)
            except OSError:
                pass
            if ensure_source(base, headers):
                open(marker, "w").write("1")
                try:
                    _post(base + "/ingest/claude_code", body, headers)
                    return True
                except Exception:
                    return False
        return False
    except Exception as e:
        if _timed_out(e):
            _size_failure = "slow"
        return False


def ensure_source(base: str, headers: dict) -> bool:
    """Create the push-mode claude_code source if it doesn't exist. 409 = already there = fine."""
    body = json.dumps({"name": "claude_code", "connector": "claude_code",
                       "poll": "10s", "config": {"push": True}}).encode()
    try:
        _post(base + "/api/sources", body, _auth_only(headers))
        return True
    except urllib.error.HTTPError as e:
        return e.code == 409          # already exists
    except Exception:
        return False


# ── the flow tool call, seen in the transcript ───────────────────────────────

def _is_flow_tool(name) -> bool:
    name = str(name or "")
    return name == FLOW_TOOL or name.endswith("__" + FLOW_TOOL)


def flow_call(obj: dict):
    """The flow a `set_session_flow` tool_use line asks for, or None when the line is not one."""
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    for b in content:
        if isinstance(b, dict) and b.get("type") == "tool_use" and _is_flow_tool(b.get("name")):
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            return str(inp.get("flow", CHALLENGER) or "").strip()
    return None


def person_queued(obj: dict):
    """A message the person typed while a turn was running, as the `user` line it would be between
    turns (TR-522). Claude Code keeps it only as a queued_command attachment, which is otherwise
    bookkeeping; without this, desk_answer never finds the person's reply. Messages from other
    sessions and task notifications queue the same way and stay out. None for any other line."""
    att = obj.get("attachment") if obj.get("type") == "attachment" else None
    if not isinstance(att, dict) or att.get("type") != "queued_command":
        return None
    origin = att.get("origin") if isinstance(att.get("origin"), dict) else {}
    if att.get("commandMode") != "prompt" or origin.get("kind") != "human":
        return None
    prompt = att.get("prompt")
    if not prompt or not isinstance(prompt, (str, list)):
        return None
    line = {k: v for k, v in obj.items() if k not in ("attachment", "type")}
    line.update(type="user", queued=True, timestamp=att.get("timestamp") or obj.get("timestamp"),
                message={"role": "user", "content": prompt})
    return line


def process_lines(raw_lines: list, flow: str, hook: dict) -> tuple:
    """Stamp `flow` on each line of a marked session and react to a set_session_flow call: update
    the flow, and emit a synthetic session_flow line right after the call so Tares records the
    change. Returns (objects to ship, the flow after these lines, whether it changed)."""
    out, changed = [], False
    for raw in raw_lines:
        try:
            o = json.loads(raw)
        except Exception:
            continue
        if isinstance(o, dict):
            o = person_queued(o) or o
        if not isinstance(o, dict) or o.get("type") in BOOKKEEPING:
            continue
        asked = flow_call(o)
        switched = asked is not None and asked != flow
        if switched:
            flow, changed = asked, True
        if flow:
            o["flow"] = flow   # the call line itself is the first stamped line
        out.append(o)
        if switched:
            line = synthetic_line(hook, "session_flow", flow)
            line["flow"] = flow   # kept even when empty: Tares renders it as "cleared"
            out.append(line)
    return out, flow, changed


# ── the Tares project a session works in (TR-405), seen in the transcript ────
# A session works in a project once it creates one (create_project) or a Tares tool names one
# (`project` in the call). The shipper stamps `tares_project` on every line from then on and writes
# a synthetic `session_project` line, which ties the whole session (the lines before included) to
# the project on Tares.

def _tares_tool(name) -> str:
    """The Tares tool a tool_use block calls ("" when it is not a Tares tool). Claude Code names
    MCP tools mcp__<server>__<tool>; the server is `tares`, or `plugin_<plugin>_tares` from a
    plugin (`plugin_tares_tares`, or a copy installed under another name)."""
    parts = str(name or "").split("__")
    if len(parts) >= 3 and parts[0] == "mcp" and (
            parts[1] == "tares" or (parts[1].startswith("plugin_") and parts[1].endswith("_tares"))):
        return parts[-1]
    return ""


# The sentences Tares hands the person to start or pick up a build (the project page, the start
# doc): pasted into a session, they tie it to the project before it calls a single tool (TR-474).
_ENTRY = re.compile(r'^\s*(?:Work on|Pick up) Tares project "([^"\n]+)"')


def entry_project(obj: dict):
    """The project a person's "Work on / Pick up Tares project "X"" message names, or None."""
    if obj.get("type") != "user":
        return None
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = msg.get("content")
    if isinstance(content, list):
        content = next((b.get("text") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"), None)
    m = _ENTRY.match(content) if isinstance(content, str) else None
    return m.group(1).strip() if m else None


def project_call(obj: dict):
    """The project a Tares tool_use line works in, or a person's build sentence names, or None."""
    entry = entry_project(obj)
    if entry:
        return entry
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    content = msg.get("content")
    if not isinstance(content, list):
        return None
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_use"):
            continue
        tool = _tares_tool(b.get("name"))
        if not tool:
            continue
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        ref = inp.get("name") if tool == "create_project" else inp.get("project")
        ref = str(ref or "").strip()
        if ref:
            return ref
    return None


def project_marker(data_dir: str, session_id: str) -> str:
    return flow_marker(data_dir, session_id)[:-len(".flow")] + ".project"


def read_project(data_dir: str, session_id: str) -> str:
    try:
        return open(project_marker(data_dir, session_id)).read().strip()
    except OSError:
        return ""


def write_project(data_dir: str, session_id: str, project: str) -> None:
    with open(project_marker(data_dir, session_id), "w") as f:
        f.write(project)


def stamp_project(objs: list, project: str, hook: dict) -> tuple:
    """Stamp `tares_project` on each line once the session works in a project, and write a
    `session_project` line after the call that named a new one. Returns (objects, project,
    whether it changed)."""
    out, changed = [], False
    for o in objs:
        asked = project_call(o)
        switched = asked is not None and asked != project
        if switched:
            project, changed = asked, True
        if project:
            o["tares_project"] = project
        out.append(o)
        if switched:
            sp = synthetic_line(hook, "session_project", o.get("flow") or "",
                                tares_project=project)
            if o.get("timestamp"):
                sp["timestamp"] = o["timestamp"]   # right after the call, on the session's clock
            out.append(sp)
    return out, project, changed


# ── the tares-factory station a session plays (TR-418), from its environment ──
# A crew station's settings set FACTORY_STATION (its stable name), FACTORY_ROLE, FACTORY_PARENT
# (who started it), FACTORY_CREW (the crew's name, TR-541) and, for builders, TARES_PROJECT. The hooks inherit them. The
# machine (`hostname -s`, or FACTORY_MACHINE, TR-628) is stamped next to the crew. The shipper sends one
# `session_station` line per session, ties the session to TARES_PROJECT from its first line, and
# stamps the station on every line, so Tares groups a station's sessions (revivals included)
# without the session calling a Tares tool.

def machine_name() -> str:
    """The machine the crew runs on: FACTORY_MACHINE, else `hostname -s` (the host name up to the
    first dot)."""
    return (os.environ.get("FACTORY_MACHINE", "").strip()
            or socket.gethostname().split(".")[0].strip())


def station_env() -> dict:
    st = os.environ.get("FACTORY_STATION", "").strip()
    if not st:
        return {}
    out = {"station": st, "role": os.environ.get("FACTORY_ROLE", "").strip(),
           "parent": os.environ.get("FACTORY_PARENT", "").strip()}
    crew = os.environ.get("FACTORY_CREW", "").strip()
    if crew:
        out["crew"] = crew
    machine = machine_name()
    if machine:
        out["machine"] = machine
    return out


def env_project() -> str:
    return os.environ.get("TARES_PROJECT", "").strip()


def station_marker(data_dir: str, session_id: str) -> str:
    return flow_marker(data_dir, session_id)[:-len(".flow")] + ".station"


def announce(cfg: dict, hook: dict, flow: str) -> None:
    """The session's station and its TARES_PROJECT, each sent once (a marker per session, written
    only after a successful ship)."""
    sid = str(hook.get("session_id") or "")
    if not sid:
        return
    data_dir = cfg["data_dir"]
    lines = []
    st = station_env()
    mark = station_marker(data_dir, sid)
    if st and not os.path.exists(mark):
        line = synthetic_line(hook, "session_station", flow, **st)
        if env_project():
            line["tares_project"] = env_project()
        lines.append(line)
    proj = env_project()
    if proj and not read_project(data_dir, sid):
        lines.append(synthetic_line(hook, "session_project", flow, tares_project=proj,
                                    from_env=True))
    if not lines:
        return
    for o in lines:
        if st:
            o["factory_station"] = st["station"]
            if st.get("crew"):
                o["factory_crew"] = st["crew"]
            if st.get("machine"):
                o["factory_machine"] = st["machine"]
    if ship_lines(cfg, lines):
        if st:
            open(mark, "w").write(st["station"])
        if proj and not read_project(data_dir, sid):
            write_project(data_dir, sid, proj)


def stamp_station(objs: list) -> list:
    st = station_env()
    if st:
        for o in objs:
            o["factory_station"] = st["station"]
            if st.get("crew"):
                o["factory_crew"] = st["crew"]
            if st.get("machine"):
                o["factory_machine"] = st["machine"]
    return objs


# ── subagent transcripts (TR-434) ─────────────────────────────────────────────
# Claude Code keeps each subagent's transcript next to the session's own:
# <transcript without .jsonl>/subagents/agent-<id>.jsonl, with agent-<id>.meta.json holding its
# type and the task's description. A builder writes its code through subagents, so without them
# Tares sees a session that only delegates. Their lines ship under the parent session, tagged with
# the subagent, each file from its own offset, capped per subagent.

MAX_SUBAGENT_LINES = 3000
MAX_SUBAGENT_READ = 4 * 1024 * 1024   # bytes read from one subagent file per hook


def subagent_files(transcript: str) -> list:
    """[(path, agent id)] of the session's subagent transcripts, oldest name first."""
    if not transcript or not transcript.endswith(".jsonl"):
        return []
    d = os.path.join(transcript[:-len(".jsonl")], "subagents")
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return []
    return [(os.path.join(d, n), n[len("agent-"):-len(".jsonl")]) for n in names
            if n.startswith("agent-") and n.endswith(".jsonl")]


def subagent_meta(path: str) -> dict:
    try:
        m = json.load(open(path[:-len(".jsonl")] + ".meta.json"))
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def ship_subagents(cfg: dict, hook: dict, flow: str, project: str) -> None:
    sid = str(hook.get("session_id") or "")
    for path, agent in subagent_files(hook.get("transcript_path") or ""):
        key = hashlib.sha1(path.encode()).hexdigest()[:16]
        off_dir = os.path.join(cfg["data_dir"], "offsets")
        os.makedirs(off_dir, exist_ok=True)
        off_file = os.path.join(off_dir, key + ".off")
        cnt_file = os.path.join(off_dir, key + ".count")
        try:
            offset = int(open(off_file).read().strip())
        except Exception:
            offset = 0
        try:
            count = int(open(cnt_file).read().strip())
        except Exception:
            count = 0
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if offset > size:          # the file was rewritten: start over
            offset, count = 0, 0
            try:
                os.remove(off_file[:-len(".off")] + ".cut")
            except OSError:
                pass
        if offset >= size:
            continue
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read(MAX_SUBAGENT_READ)   # bounded: a huge file ships over several hooks
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            if len(chunk) == MAX_SUBAGENT_READ:
                # one line larger than a whole read: step over it so the file does not stall
                with open(off_file, "w") as f:
                    f.write(str(offset + len(chunk)))
                ship_lines(cfg, [synthetic_line(hook, "subagent_truncated", flow, subagent=agent,
                                                note=f"a line of subagent {agent} was too large "
                                                     "to record")])
            continue
        meta = subagent_meta(path)
        objs = []
        for raw in chunk[: last_nl + 1].decode("utf-8", "replace").splitlines():
            try:
                o = json.loads(raw)
            except Exception:
                continue
            if not isinstance(o, dict) or o.get("type") in BOOKKEEPING:
                continue
            o.setdefault("sessionId", sid)
            o["isSidechain"] = True
            o["subagent"] = agent
            if meta.get("agentType"):
                o["subagent_type"] = str(meta["agentType"])
            if meta.get("description"):
                o["subagent_description"] = str(meta["description"])[:200]
            if flow:
                o["flow"] = flow
            if project:
                o["tares_project"] = project
            objs.append(o)
        room = max(MAX_SUBAGENT_LINES - count, 0)
        noted = os.path.exists(off_file[:-len(".off")] + ".cut")
        cut = False
        if len(objs) > room:
            objs = objs[:room]
            if not noted:
                cut = True
                objs.append(synthetic_line(hook, "subagent_truncated", flow, subagent=agent,
                                           note=f"subagent {agent} passed {MAX_SUBAGENT_LINES} "
                                                "lines; the rest is not recorded"))
        if not objs or ship_lines(cfg, stamp_station(objs)):
            with open(off_file, "w") as f:
                f.write(str(offset + last_nl + 1))
            with open(cnt_file, "w") as f:
                f.write(str(min(count + len(objs) - (1 if cut else 0), MAX_SUBAGENT_LINES)))
            if cut:
                open(off_file[:-len(".off")] + ".cut", "w").write("1")


# ── the session's own transcript (TR-809) ────────────────────────────────────
# Read from the saved offset, at most MAX_MAIN_READ bytes per hook: one POST of a long session's
# whole backlog can outlast the post timeout, fail on every hook, and leave the offset stuck. The
# offset moves after each chunk that ships, so a backlog drains over the next hooks. A single line
# larger than the read goes out alone; one larger than MAX_MAIN_LINE is stepped over with a note.
#
# A session resumed under a new id (a restart in the same conversation) gets a new file that opens
# with the whole conversation, so its first request is a full read (TR-1102). A request refused for
# its size or too slow to answer is tried again smaller in the same hook, down to one line. One
# line that is refused for its size is stepped over, as it would be refused on every hook.

MAX_MAIN_READ = 768 * 1024           # bytes read per hook: under the 1 MiB an nginx ingress accepts
SPLIT_BUDGET = 15                    # seconds a hook spends retrying a request in smaller pieces
MAX_MAIN_LINE = 8 * 1024 * 1024      # a line longer than this is not sent
SESSION_END_BUDGET = 10             # seconds SessionEnd spends draining a backlog


def _main_off_file(cfg: dict, transcript: str) -> str:
    return os.path.join(cfg["data_dir"], "offsets",
                        hashlib.sha1(transcript.encode()).hexdigest()[:16] + ".off")


def _main_offset(cfg: dict, transcript: str) -> int:
    try:
        return int(open(_main_off_file(cfg, transcript)).read().strip())
    except Exception:
        return 0


def _main_chunk(path: str, offset: int, limit: int = 0) -> tuple:
    """(complete lines to ship, the offset after them, size of a line too large to send or 0),
    reading at most `limit` bytes (MAX_MAIN_READ by default) unless one line is longer."""
    limit = limit or MAX_MAIN_READ
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(limit)
        last_nl = chunk.rfind(b"\n")
        if last_nl >= 0:
            return chunk[: last_nl + 1], offset + last_nl + 1, 0
        if len(chunk) < limit:
            return b"", offset, 0            # a line still being written: wait for its end
        # one line larger than a whole read: find its end, keeping it only while it is sendable
        line, end = chunk, offset + len(chunk)
        while True:
            more = f.read(limit)
            if not more:
                return b"", offset, 0        # not finished yet
            nl = more.find(b"\n")
            piece = more if nl < 0 else more[: nl + 1]
            end += len(piece)
            if line is not None:
                line = line + piece if len(line) + len(piece) <= MAX_MAIN_LINE else None
            if nl >= 0:
                break
    if line is None:
        return b"", end, end - offset
    return line, end, 0


def ship_main(cfg: dict, hook: dict, transcript: str, flow: str) -> tuple:
    """Ship the next chunk of the session's transcript. Returns (shipped ok, flow after it)."""
    data_dir = cfg["data_dir"]
    session_id = str(hook.get("session_id") or "")
    os.makedirs(os.path.join(data_dir, "offsets"), exist_ok=True)
    off_file = _main_off_file(cfg, transcript)
    offset = _main_offset(cfg, transcript)

    size = os.path.getsize(transcript)
    if offset > size:      # transcript rotated/truncated — start over
        offset = 0
    if offset >= size:
        return True, flow

    def step_over(skipped_bytes: int, past: int) -> bool:
        # session_warning: every Tares version renders it ("warning: ...")
        note = synthetic_line(hook, "session_warning", flow,
                              warning=f"a transcript line of {skipped_bytes} bytes was too large to record")
        project = read_project(data_dir, session_id) if session_id else ""
        if project:
            note["tares_project"] = project
        ok = ship_lines(cfg, stamp_station([note]))
        if ok:
            with open(off_file, "w") as f:
                f.write(str(past))
        return ok

    limit, deadline = MAX_MAIN_READ, time.monotonic() + SPLIT_BUDGET
    while True:
        data, new_offset, skipped = _main_chunk(transcript, offset, limit)
        if skipped:
            return step_over(skipped, new_offset), flow
        if not data:
            return True, flow
        raw_lines = data.decode("utf-8", "replace").splitlines()
        objs, after, changed = process_lines(raw_lines, flow, hook)
        project = read_project(data_dir, session_id) if session_id else ""
        objs, project, p_changed = stamp_project(objs, project, hook)
        ok = ship_lines(cfg, stamp_station(objs))
        if ok or not _size_failure:
            break
        if data.count(b"\n") <= 1:
            if _size_failure == "refused":
                return step_over(len(data), new_offset), flow
            break                                # one slow line: the next hook tries it again
        if time.monotonic() >= deadline:
            break
        limit = len(data) // 2

    flow = after
    if changed and session_id:
        write_flow(data_dir, session_id, flow)   # the local hooks gate on this
    if ok and p_changed and session_id:
        # only once shipped: a failed post retries these lines, the session_project line with them
        write_project(data_dir, session_id, project)
    if ok:
        with open(off_file, "w") as f:
            f.write(str(new_offset))   # advance only on success; failures retry next hook
    return ok, flow


# One shipper per transcript at a time: a lock file made with O_EXCL (works the same on Windows),
# taken over when older than LOCK_STALE (a hook killed while holding it). A hook that finds it held
# ships nothing: the holder, or the next hook, sends the new lines.
LOCK_STALE = 120
# seconds a hook waits for the lock: the ones after which no hook may come for a while
LOCK_WAIT = {"SessionEnd": 5, "Stop": 5, "SubagentStop": 5}


def ship_lock(cfg: dict, transcript: str, wait: float = 0) -> str:
    """The lock file's path when this process now holds the transcript's lock, else ""."""
    path = _main_off_file(cfg, transcript)[:-len(".off")] + ".lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            return path
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > LOCK_STALE:
                    os.remove(path)
                    continue
            except OSError:
                continue
        except OSError:
            return ""
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.1)


def release_lock(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def drain_main(cfg: dict, hook: dict, transcript: str, flow: str) -> None:
    """SessionEnd: no hook comes after it, so send the rest of a backlog, chunk by chunk, while
    chunks go through and time is left. Runs last, so the session's end is already recorded if
    Claude Code stops the hook early."""
    deadline = time.monotonic() + SESSION_END_BUDGET
    while time.monotonic() < deadline:
        before = _main_offset(cfg, transcript)
        ok, flow = ship_main(cfg, hook, transcript, flow)
        if not ok or _main_offset(cfg, transcript) == before:
            break
    sid = str(hook.get("session_id") or "")
    if sid:
        write_flow(cfg["data_dir"], sid, "")   # a flow call in the backlog must not revive the marker


# ── SessionStart: hand accepted memory to Claude ─────────────────────────────

def memory_context(cfg: dict, cwd: str) -> str:
    """Accepted memory for this repo (decisions on the memory source keyed by the repo name, the
    working directory's basename), as a short block Claude sees at session start. Empty on any
    failure."""
    repo = os.path.basename((cwd or "").rstrip("/")) or ""
    if not repo:
        return ""
    try:
        r = _get(f"{cfg['base']}/api/sources/agent_memory/events?limit=200",
                 _auth_only(cfg["headers"]))
        rows = json.loads(r.read().decode() or "[]")
    except Exception:
        return ""
    lines = []
    for row in rows if isinstance(rows, list) else []:
        if row.get("key") == repo and row.get("event_type") == "decision" and row.get("text"):
            lines.append(row["text"].strip())
    if not lines:
        return ""
    seen, uniq = set(), []
    for l in lines:
        if l not in seen:
            seen.add(l); uniq.append(l)
    return ("Memory from earlier Tares sessions in this repo (accepted by the user):\n"
            + "\n".join(f"- {l}" for l in uniq[:20]))


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    try:
        hook = json.load(sys.stdin)
    except Exception:
        return

    if not _truthy(_opt("stream_sessions", "true")):
        return

    cfg = config()
    base, headers, data_dir = cfg["base"], cfg["headers"], cfg["data_dir"]
    marker = os.path.join(data_dir, "ensured-" + hashlib.sha1(base.encode()).hexdigest()[:12])

    # ensure the source exists (once per Tares URL; cheap fast-path via a marker file)
    if not os.path.exists(marker):
        if ensure_source(base, headers):
            open(marker, "w").write("1")

    event = hook.get("hook_event_name")
    session_id = str(hook.get("session_id") or "")

    # SessionStart provisions the source and hands Claude the accepted memory; nothing to ship yet
    # (the flow marker is left alone here: a resumed session keeps its mark, and SessionEnd
    # removes it)
    if event == "SessionStart":
        announce(cfg, hook, read_flow(data_dir, session_id) if session_id else "")
        ctx = memory_context(cfg, hook.get("cwd") or "")
        if ctx:
            print(ctx)
        return

    transcript = hook.get("transcript_path")
    flow = read_flow(data_dir, session_id) if session_id else ""
    if event == "SessionEnd" and session_id:
        # the end first: Claude Code gives this hook little time on exit, and shipping the last
        # transcript lines and the subagents' can take longer. A session that exited must not
        # stay "waiting" on Tares (TR-473).
        report_state(cfg, hook, event, flow)
    announce(cfg, hook, flow)   # a session the plugin joined after it started

    shipped_ok = True
    lock = ""
    if transcript and os.path.isfile(transcript):
        # the async hooks of one session run side by side; only one ships its transcript at a
        # time, or two read the same offset and post the same lines (each call listed twice)
        # a turn's end waits a little: no hook may come after it for a long time, and its last
        # lines (a person's answer the desk looks for) must not wait for the next turn
        lock = ship_lock(cfg, transcript, wait=LOCK_WAIT.get(event, 0))
    try:
        if lock:
            shipped_ok, flow = ship_main(cfg, hook, transcript, flow)
        if lock and session_id and shipped_ok:
            ship_subagents(cfg, hook, flow, read_project(data_dir, session_id))
    except BaseException:
        release_lock(lock) if lock else None
        raise
    if lock and event != "SessionEnd":
        release_lock(lock)
    if session_id and event != "SessionEnd":
        # whatever became of the transcript: a session whose lines can't get through is still alive
        report_state(cfg, hook, event, flow)

    if event == "SessionEnd" and session_id and flow:
        # the line the challenger_session_ended trigger watches; the marker goes with the session
        ship_lines(cfg, [synthetic_line(hook, "session_end", flow)])
        write_flow(data_dir, session_id, "")

    if event == "SessionEnd" and lock:
        try:
            if shipped_ok:
                drain_main(cfg, hook, transcript, flow)
        finally:
            release_lock(lock)


# ── the session's state (working / idle / waiting / ended), from the hook that fired ─
# Sent as a `session_state` line when it changes, so Tares can say a session stopped, even when
# its last turn ran no tool. `waiting` means the person is needed: a permission prompt, a
# question or an elicitation. A turn that ended (Stop, and Claude Code's idle notice ~60s later)
# is only `idle`: the session may be waiting on its own Monitor or on a peer, and resumes by
# itself. A crash sends nothing; Tares notices that from the silence.

IDLE_REASON = "the turn ended"
NEEDS_PERSON = ("permission_prompt", "elicitation_dialog", "elicitation_url_dialog")


def notification_type(hook: dict) -> str:
    """The Notification's type. Older Claude Code versions send none; their message says it."""
    kind = str(hook.get("notification_type") or "")
    if kind:
        return kind
    message = str(hook.get("message") or "").lower()
    if "needs your permission" in message:
        return "permission_prompt"
    if "waiting for your input" in message:
        return "idle_prompt"
    return ""


def state_for(event: str, hook: dict) -> tuple:
    """(state, reason) a hook event means, or (None, None) when it says nothing new."""
    if event in ("UserPromptSubmit", "PostToolUse"):
        return "working", ""
    if event == "PreToolUse" and hook.get("tool_name") == "AskUserQuestion":
        return "waiting", "asking you a question"
    if event == "PermissionRequest":
        # fires the moment Claude asks; the permission_prompt Notification comes ~6 s later, and a
        # background session may not get it at all
        tool = str(hook.get("tool_name") or "a tool")
        return "waiting", f"Claude Code needs your permission to use {tool}"[:300]
    if event == "Stop":
        return "idle", IDLE_REASON
    if event == "Notification":
        kind = notification_type(hook)
        if kind in NEEDS_PERSON:
            return "waiting", str(hook.get("message") or "Claude Code needs your attention")[:300]
        if kind == "idle_prompt":
            return "idle", IDLE_REASON
        return None, None
    if event == "SessionEnd":
        return "ended", str(hook.get("reason") or "")[:100]
    return None, None


def state_marker(data_dir: str, session_id: str) -> str:
    return flow_marker(data_dir, session_id)[:-len(".flow")] + ".state"


def report_state(cfg: dict, hook: dict, event: str, flow: str) -> None:
    state, reason = state_for(event, hook)
    if state is None:
        return
    sid = str(hook.get("session_id") or "")
    path = state_marker(cfg["data_dir"], sid)
    key = f"{state}|{reason}"
    try:
        if open(path).read() == key:
            return   # unchanged: one line per change, not per tool call
    except OSError:
        pass
    project = read_project(cfg["data_dir"], sid)
    line = synthetic_line(hook, "session_state", flow, state=state, reason=reason)
    if project:
        line["tares_project"] = project
    if ship_lines(cfg, stamp_station([line])):
        if state == "ended":
            try:
                os.remove(path)
            except OSError:
                pass
        else:
            with open(path, "w") as f:
                f.write(key)


if __name__ == "__main__":
    try:
        main()
    except Exception:   # a hook must never take the session down
        pass
