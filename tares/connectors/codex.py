"""Codex station sessions (TR-1595): what a crew's recorder sends of a Codex station, turned into session lines.

A Claude Code session reaches Tares as its transcript lines, posted to /ingest/claude_code by the plugin's
hooks (`claude_code.py`, `claude-plugin/scripts/ship.py`). A Codex station has no such hooks and its
transcript file is Codex's own business. The crew's recorder (`tares.crew.codex_record`) reads the station by
visits and posts what it found to /ingest/codex, one JSON object per fact (the kinds are below). `session_lines`
turns those facts into lines of the shape the cell already stores, and the daemon hands them to the cell's
session source (the one `/ingest/claude_code` feeds). So a Codex station's session is in the same tables, is
read by the same readers and shows on the same pages as a Claude Code one, and nothing here writes a store.

The facts, each with `sessionId` (the Codex conversation), `timestamp`, and the station's stamps
(`factory_station`, `factory_crew`, `factory_machine`, `tares_project`, `cwd`, `model`):

  session   the conversation is a station's: `station`, `role`, `parent`              -> session_station
            (and session_project when it has a project)
  state     `state` (working, idle, waiting, ended) and `reason`                      -> session_state
  start     a command the guard let through, before it ran: `call`, `tool`,           -> a tool_use line
            `command`, `cwd` (the guard's record, `tares.crew.codex_guard`)
  item      one item of a turn as Codex lists it once it has ended: a message, a      -> a line, or the
            command with its real exit code, folder and output, an MCP call           tool_result of its start
  unknown   a command that started and whose end will never be known: `call`, `why`   -> a tool_result line
  usage     the tokens of one model reply, when Codex wrote them down: `n`, `usage`   -> session_usage

What makes a repeat harmless: every line carries a `uuid` made of the conversation and the item (or call) it
is about, and a command's row in `session_commands` is keyed by `codex:<conversation>:<call>`. The recorder
sends each fact once; when it sends one again (a post whose answer was lost), the facts tables take it once.

A command's line carries `codex`: {exit_code, cwd, outcome, command, duration_ms}. `outcome` is `started`
(no end yet), `ended`, `failed` or `declined` (Codex's own status, when it gave no exit code) or `unknown`.
`tares.session_ingest` writes these to the command's row, and `tares.factory.match_check` lets no command
verify a claim unless it ended.

Pure: no store, no network. A fact that is not one of the kinds, or lacks what its kind needs, is dropped.
"""
from __future__ import annotations

from datetime import datetime, timezone

MARK = "codex"                 # a fact's `type`, and the key of what a line keeps of Codex's own
CLOSE_STDIN = "exec </dev/null\n"   # the line the guard puts in front of a command (tares.crew.codex_guard)
STAMPS = ("cwd", "gitBranch", "factory_station", "factory_crew", "factory_machine", "tares_project", "flow")
STATES = ("working", "idle", "waiting", "ended")
OUTCOMES = ("started", "ended", "failed", "declined", "unknown")
CREW = {"kind": "crew"}        # a station's prompts come from its crew, never from a person typing


def call_id(session: str, call: str) -> str:
    """The id of a command or MCP call in the session's lines and in `session_commands`: scoped to its
    conversation, so the same item sent twice is one row and two conversations never share one."""
    return f"{MARK}:{session}:{call}"


def asked(command) -> str:
    """A command as the station asked for it: without the guard's added line, and without the login shell
    Codex wraps it in (`/bin/zsh -lc '<text>'`)."""
    if not isinstance(command, str):
        return ""
    if command.startswith(CLOSE_STDIN):
        return command[len(CLOSE_STDIN):]
    at = command.find(CLOSE_STDIN)
    if at > 0 and command[at - 1] in "'\"" and command.endswith(command[at - 1]):
        inner = command[at + len(CLOSE_STDIN):-1]
        return inner.replace("'\\''", "'") if command[at - 1] == "'" else inner
    return command


def item_command(item: dict) -> str:
    """The command of a `commandExecution` item, as asked. Codex keeps the inner text in `commandActions`
    when it reads the command as one action; else it is taken out of the wrapped `command`."""
    actions = item.get("commandActions")
    if isinstance(actions, list) and len(actions) == 1 and isinstance(actions[0], dict) \
            and isinstance(actions[0].get("command"), str) and actions[0]["command"].startswith(CLOSE_STDIN):
        return asked(actions[0]["command"])
    return asked(item.get("command"))


def _iso(ms) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _base(fact: dict, typ: str, key: str, at: str | None = None) -> dict:
    line = {"type": typ, "sessionId": str(fact["sessionId"]), "uuid": f"{MARK}:{fact['sessionId']}:{key}",
            "timestamp": at or fact.get("timestamp"), "version": "tares-codex", "runtime": MARK}
    for k in STAMPS:
        if fact.get(k):
            line[k] = fact[k]
    return line


def _tool_use(fact: dict, call: str, name: str, tool_input: dict, at: str | None, extra: dict) -> dict:
    line = _base(fact, "assistant", f"{call}:start", at)
    line["message"] = {"role": "assistant", "model": fact.get("model") or None,
                       "content": [{"type": "tool_use", "id": call_id(str(fact["sessionId"]), call),
                                    "name": name, "input": tool_input}]}
    line[MARK] = {"turn": fact.get("turn"), **extra}
    return line


def _tool_result(fact: dict, call: str, text: str, failed: bool, at: str | None, extra: dict) -> dict:
    line = _base(fact, "user", f"{call}:end", at)
    line["message"] = {"role": "user", "content": [{"type": "tool_result",
                                                    "tool_use_id": call_id(str(fact["sessionId"]), call),
                                                    "content": text, "is_error": bool(failed)}]}
    line[MARK] = {"turn": fact.get("turn"), **extra}
    return line


def _texts(content) -> str:
    """The text of a message's content: Codex gives a string, or a list of parts with `text`."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("text"))
    return ""


def _mcp_text(item: dict) -> str:
    err = item.get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    result = item.get("result")
    return _texts(result.get("content")) if isinstance(result, dict) else ""


def _session(fact: dict) -> list:
    if not fact.get("station"):
        return []
    line = _base(fact, "session_station", "station")
    line.update(station=fact["station"], role=fact.get("role") or "", parent=fact.get("parent") or "")
    for k in ("crew", "machine"):
        if fact.get(f"factory_{k}"):
            line[k] = fact[f"factory_{k}"]
    out = [line]
    if fact.get("tares_project"):
        out.append({**_base(fact, "session_project", "project"), "from_env": True})
    return out


def _state(fact: dict) -> list:
    if fact.get("state") not in STATES:
        return []
    line = _base(fact, "session_state", f"state:{fact.get('timestamp')}")
    line.update(state=fact["state"], reason=str(fact.get("reason") or ""))
    return [line]


def _start(fact: dict) -> list:
    call = str(fact.get("call") or "")
    if not call:
        return []
    tool = str(fact.get("tool") or "Bash")
    if tool == "Bash":
        extra = {"outcome": "started", "cwd": fact.get("command_cwd") or fact.get("cwd")}
        if fact.get("cut"):
            extra["cut"] = True
        return [_tool_use(fact, call, "Bash", {"command": asked(fact.get("command"))}, None, extra)]
    args = fact.get("arguments")
    return [_tool_use(fact, call, tool, args if isinstance(args, dict) else {}, None, {})]


def _item(fact: dict) -> list:
    item = fact.get("item")
    if not isinstance(item, dict) or not item.get("id"):
        return []
    kind, iid = item.get("type"), str(item["id"])
    began, ended = _iso(item.get("startedAtMs")), _iso(item.get("completedAtMs"))
    if kind == "userMessage":
        text = _texts(item.get("content"))
        if not text:
            return []
        line = _base(fact, "user", iid, ended or began)
        line.update(origin=dict(CREW), message={"role": "user", "content": text})
        return [line]
    if kind in ("agentMessage", "plan"):
        text = str(item.get("text") or "")
        if not text:
            return []
        line = _base(fact, "assistant", iid, ended or began)
        line["message"] = {"role": "assistant", "id": iid, "model": fact.get("model") or None,
                           "content": [{"type": "text", "text": ("Plan:\n" + text) if kind == "plan" else text}]}
        return [line]
    if kind == "commandExecution":
        code = item.get("exitCode") if isinstance(item.get("exitCode"), int) else None
        status = item.get("status")
        outcome = _outcome(code, status)
        extra = {"exit_code": code, "cwd": item.get("cwd") or None, "outcome": outcome,
                 "command": item_command(item), "duration_ms": item.get("durationMs"), "status": status}
        failed = bool(code) or outcome != "ended"
        return [_tool_result(fact, iid, str(item.get("aggregatedOutput") or ""), failed, ended or began, extra)]
    if kind == "mcpToolCall":
        failed = item.get("status") == "failed" or bool(item.get("error"))
        return [_tool_result(fact, iid, _mcp_text(item), failed, ended or began,
                             {"duration_ms": item.get("durationMs"), "status": item.get("status")})]
    if kind == "fileChange":
        changes = [{"path": c.get("path"), "kind": c.get("kind")} for c in item.get("changes") or []
                   if isinstance(c, dict)]
        return [_tool_use(fact, iid, "apply_patch", {"changes": changes}, began or ended, {}),
                _tool_result(fact, iid, str(item.get("status") or ""), item.get("status") in ("failed", "declined"),
                             ended or began, {"status": item.get("status")})]
    return []


def _outcome(code, status) -> str:
    """How a command ended, from what Codex says of it: with an exit code it ended; without one, Codex's own
    word for a command that failed to run or was declined; anything else is not known."""
    if code is not None or status == "completed":
        return "ended"
    return status if status in ("failed", "declined") else "unknown"


def _unknown(fact: dict) -> list:
    call = str(fact.get("call") or "")
    if not call:
        return []
    why = str(fact.get("why") or "its end was not recorded")
    return [_tool_result(fact, call, f"outcome unknown: {why}", True, None, {"exit_code": None, "outcome": "unknown"})]


def _number(v):
    """A count Codex reported, or None for one it did not (never 0 in its place)."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _usage(fact: dict) -> list:
    """One reply's tokens as a `session_usage` line. Codex counts the cached tokens inside its input; the
    cell keeps them apart, as Claude Code reports them. A count Codex did not give stays out of the line.
    The cost is not here: the cell prices a reply by its model, and prices none it has no price for."""
    usage, n = fact.get("usage"), fact.get("n")
    if not isinstance(usage, dict) or not isinstance(n, int):
        return []
    total, cached = _number(usage.get("input_tokens")), _number(usage.get("cached_input_tokens"))
    out = {}
    if total is not None:
        out["input_tokens"] = max(total - (cached or 0), 0)
    if cached is not None:
        out["cache_read_input_tokens"] = cached
    if _number(usage.get("cache_write_input_tokens")) is not None:
        out["cache_creation_input_tokens"] = usage["cache_write_input_tokens"]
    if _number(usage.get("output_tokens")) is not None:
        out["output_tokens"] = usage["output_tokens"]
    if not out:
        return []
    line = _base(fact, "session_usage", f"usage:{n}")
    line.update(message_id=f"{MARK}:usage:{n}", model=fact.get("model") or None, usage=out)
    return [line]


KINDS = {"session": _session, "state": _state, "start": _start, "item": _item, "unknown": _unknown, "usage": _usage}


def session_lines(body) -> list:
    """The session lines of what a recorder posted: `body` is one fact or a list of them."""
    facts = body if isinstance(body, list) else [body]
    out = []
    for fact in facts:
        if not (isinstance(fact, dict) and fact.get("type") == MARK and fact.get("sessionId")):
            continue
        make = KINDS.get(str(fact.get("kind") or ""))
        if make:
            out.extend(make(fact))
    return out
