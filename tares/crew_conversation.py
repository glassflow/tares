"""A station's session as a conversation (TR-1177): what the Crew page's "Show what it did" shows.

The session's stored lines (events of the Claude Code source, keyed by session) are read with their lossless
payload, the transcript line itself, and turned into items, oldest first:

  {"kind": "said", "at", "text"}                          what the station said
  {"kind": "asked", "at", "text", "from", "quiet"}       a message to it; `from` names the sender, `quiet` is
                                                          true for what the factory itself sends
  {"kind": "tool", "at", "id", "name", "summary", "input", "output", "failed", "ms", "more"}
                                                          one tool call with its own result, matched by the
                                                          call's id; input is JSON text, output the result's
                                                          text, both cut at CAP characters (`more` says the
                                                          output was cut: GET .../tool/{id} gives it whole)
  {"kind": "divider", "at", "text"}                      a session event (started, compacted, a warning)

Who sent a message is read from the line: another session's message comes wrapped as
`<cross-session-message ... from-name="crew-orchestrator">`, a sub-agent's hand-back as `<agent-message>`, a
prompt someone typed into the session has `promptSource: "typed"`. Anything else sent to the session (a tick,
a restart with new permissions, a wake) is the factory's own, shown quietly. Lines the session gives itself
(a skill's text, a command's expansion, a task notification) are not messages and are left out.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

CAP = 4000            # characters of a tool's input or output in the list
SUMMARY = 120         # characters of a tool's one-line summary
FACTORY = "The factory"
PERSON = "Typed in the session"
CONTINUED = "This session is being continued from a previous conversation"

XSESS = re.compile(r'<cross-session-message\b([^>]*)>(.*?)(?:</cross-session-message>|$)', re.S)
FROM_NAME = re.compile(r'from-name="([^"]*)"')
AGENT_MSG = re.compile(r'<agent-message\b[^>]*>(.*?)(?:</agent-message>|$)', re.S)
SKIP_TAGS = ("<task-notification>", "<local-command-stdout>", "<local-command-caveat>", "<system-reminder>")
COMMAND = re.compile(r"<command-name>([^<]*)</command-name>.*?(?:<command-args>([^<]*)</command-args>)?", re.S)

# a tool's one-line summary: the input field that says most about the call
SUMMARY_FIELDS = ("command", "file_path", "path", "pattern", "query", "url", "description", "prompt", "to",
                  "message", "subject", "name")


def _time(v):
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _iso(v) -> str | None:
    t = _time(v)
    return t.isoformat() if t else (str(v) if v else None)


def _texts(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
            elif b.get("type") == "image":
                parts.append("[an image]")
        return "\n".join(parts)
    return "" if content is None else str(content)


def summary(name: str, inp) -> str:
    """One line about a call: its command, its file, its pattern... else the start of its input."""
    if isinstance(inp, dict):
        for f in SUMMARY_FIELDS:
            v = inp.get(f)
            if isinstance(v, str) and v.strip():
                return " ".join(v.split())[:SUMMARY]
        text = json.dumps(inp, ensure_ascii=False)
    else:
        text = str(inp or "")
    return " ".join(text.split())[:SUMMARY]


def asked(o: dict, text: str) -> dict | None:
    """A message sent to the station, with its sender; None for a line the session gives itself."""
    if o.get("isMeta") and o.get("turnOrigin") != "peer":
        return None
    if any(t in text for t in SKIP_TAGS) and "<cross-session-message" not in text:
        return None
    m = XSESS.search(text)
    if m:
        who = FROM_NAME.search(m.group(1))
        return {"text": m.group(2).strip(), "from": who.group(1) if who and who.group(1) else "Another session",
                "quiet": False}
    m = AGENT_MSG.search(text)
    if m:
        return {"text": m.group(1).strip(), "from": "A helper", "quiet": False}
    m = COMMAND.search(text)
    if m:
        text = f"{m.group(1).strip()} {(m.group(2) or '').strip()}".strip()
    if o.get("promptSource") == "typed" or o.get("turnOrigin") == "human" and o.get("promptSource") != "system":
        return {"text": text.strip(), "from": PERSON, "quiet": False}
    return {"text": text.strip(), "from": FACTORY, "quiet": True}


def build(rows: list[dict]) -> tuple[list[dict], dict]:
    """rows: {event_type, event_time, payload, text} oldest first. Returns (items, full outputs by call id)."""
    items: list[dict] = []
    calls: dict[str, dict] = {}
    full: dict[str, str] = {}
    for r in rows:
        o = r.get("payload")
        if isinstance(o, str):
            try:
                o = json.loads(o)
            except ValueError:
                o = {}
        if not isinstance(o, dict):
            o = {}
        at = r.get("event_time")
        typ = o.get("type") or r.get("event_type")
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        content = msg.get("content")
        if typ == "assistant":
            said = _texts(content)
            if said:
                items.append({"kind": "said", "at": _iso(at), "text": said})
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tid = str(b.get("id") or "")
                    inp = b.get("input")
                    raw = json.dumps(inp, ensure_ascii=False, indent=2) if isinstance(inp, (dict, list)) else str(inp or "")
                    item = {"kind": "tool", "at": _iso(at), "id": tid, "name": str(b.get("name") or "tool"),
                            "summary": summary(str(b.get("name") or ""), inp), "input": raw[:CAP],
                            "output": None, "failed": False, "ms": None, "more": False, "_t": _time(at)}
                    items.append(item)
                    if tid:
                        calls[tid] = item
        elif typ == "user":
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                                 for b in content):
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    tid = str(b.get("tool_use_id") or "")
                    out = _result_text(b.get("content"))
                    call = calls.get(tid)
                    if call is None:
                        continue   # its call is before the lines read: nothing to put it in
                    full[tid] = out
                    call["output"] = out[:CAP]
                    call["more"] = len(out) > CAP
                    call["failed"] = bool(b.get("is_error"))
                    t, t0 = _time(at), call.get("_t")
                    if t and t0:
                        call["ms"] = max(0, int((t - t0).total_seconds() * 1000))
                continue
            text = _texts(content)
            if not text.strip():
                continue
            if text.startswith(CONTINUED):
                items.append({"kind": "divider", "at": _iso(at), "text": "Continued after its memory was compacted"})
                continue
            a = asked(o, text)
            if a and a["text"]:
                items.append({"kind": "asked", "at": _iso(at), **a})
        elif typ == "system" and o.get("subtype") == "compact_boundary":
            items.append({"kind": "divider", "at": _iso(at), "text": "Memory compacted"})
        elif typ == "session_station":
            items.append({"kind": "divider", "at": _iso(at), "text": "Session started"})
        elif typ == "session_warning":
            items.append({"kind": "divider", "at": _iso(at), "text": str(o.get("warning") or "")[:300]})
        elif str(typ).startswith("challenge_"):
            items.append({"kind": "asked", "at": _iso(at), "text": str(r.get("text") or "")[:CAP],
                          "from": "The challenger", "quiet": False})
    for it in items:
        it.pop("_t", None)
    return items, full
