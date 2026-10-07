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
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

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


def _post(url: str, data: bytes, headers: dict, timeout: float = 5):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def _get(url: str, headers: dict, timeout: float = 3):
    req = urllib.request.Request(url, headers=headers, method="GET")
    return urllib.request.urlopen(req, timeout=timeout)


def _auth_only(headers: dict) -> dict:
    h = {"Content-Type": "application/json"}
    if "Authorization" in headers:
        h["Authorization"] = headers["Authorization"]
    return h


# ── shared with challenger.py ────────────────────────────────────────────────

def config() -> dict:
    """Where Tares is, how to talk to it, and where this plugin keeps its state."""
    base = (_opt("tares_url") or "http://127.0.0.1:8787").rstrip("/")
    token = (_opt("access_token") or "").strip()
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


def _ship_body(cfg: dict, body: bytes) -> bool:
    base, headers = cfg["base"], cfg["headers"]
    marker = os.path.join(cfg["data_dir"], "ensured-" + hashlib.sha1(base.encode()).hexdigest()[:12])
    try:
        _post(base + "/ingest/claude_code", body, headers)
        return True
    except urllib.error.HTTPError as e:
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
    except Exception:
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
        if not isinstance(o, dict):
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


def project_call(obj: dict):
    """The project a Tares tool_use line works in, or None."""
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
        ctx = memory_context(cfg, hook.get("cwd") or "")
        if ctx:
            print(ctx)
        return

    transcript = hook.get("transcript_path")
    flow = read_flow(data_dir, session_id) if session_id else ""

    shipped_ok = True
    if transcript and os.path.isfile(transcript):
        off_dir = os.path.join(data_dir, "offsets")
        os.makedirs(off_dir, exist_ok=True)
        off_file = os.path.join(off_dir, hashlib.sha1(transcript.encode()).hexdigest()[:16] + ".off")
        try:
            offset = int(open(off_file).read().strip())
        except Exception:
            offset = 0

        size = os.path.getsize(transcript)
        if offset > size:      # transcript rotated/truncated — start over
            offset = 0
        if offset < size:
            with open(transcript, "rb") as f:
                f.seek(offset)
                chunk = f.read()
            last_nl = chunk.rfind(b"\n")      # only ship complete lines
            if last_nl >= 0:
                raw_lines = chunk[: last_nl + 1].decode("utf-8", "replace").splitlines()
                new_offset = offset + last_nl + 1
                objs, flow, changed = process_lines(raw_lines, flow, hook)
                if changed and session_id:
                    write_flow(data_dir, session_id, flow)   # the local hooks gate on this
                project = read_project(data_dir, session_id) if session_id else ""
                objs, project, p_changed = stamp_project(objs, project, hook)
                shipped_ok = ship_lines(cfg, objs)
                if shipped_ok and p_changed and session_id:
                    # only once shipped: a failed post retries these lines, the session_project
                    # line with them
                    write_project(data_dir, session_id, project)
                if shipped_ok:
                    with open(off_file, "w") as f:
                        f.write(str(new_offset))   # advance only on success; failures retry next hook

    if event == "SessionEnd" and session_id and flow:
        # the line the challenger_session_ended trigger watches; the marker goes with the session
        ship_lines(cfg, [synthetic_line(hook, "session_end", flow)])
        write_flow(data_dir, session_id, "")


if __name__ == "__main__":
    try:
        main()
    except Exception:   # a hook must never take the session down
        pass
