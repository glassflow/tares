"""What each Claude Code session's lines add up to, kept in the session store (P-TR-295, TR-1471).

Every claude_code line is written here as it is stored, and nowhere else (Tares 2.0, TR-1815):
`Store.append` calls `record_first`, which writes a batch's counts, lines, facts and turns in one
transaction.

- `sessions`: one row per session, the lines counted into it as they are stored (`ON CONFLICT ... DO
  UPDATE`, so concurrent batches lose nothing). The readers `Store.session_activity`,
  `Store.project_sessions`, `FactoryStore.station_sessions` and `FactoryStore.coordination_calls`
  read it (TR-1499).
- `session_lines` (TR-1478): every line as it was stored, numbered within its session by `seq`.
  `record_first` takes `seq` from the `line_count` the `sessions` upsert returns and inserts the lines
  in that transaction, so `seq` is dense and unique per session on SQLite and Postgres. A line sent
  twice is stored twice. The hourly archive (`session_archive.py`, TR-1506) moves the rows of
  sessions idle for a week into files, recorded in `session_archive_parts`, and `session_read.py`
  (TR-1507) reads a session from the table and the parts. `tares_session_lines_rows` and
  `tares_session_lines_bytes` say how big the table is.
- the facts the factory reads (TR-1477): the crew's protocol messages in `session_messages` and the
  shell commands sessions ran, with what they printed, in `session_commands`. `message_facts`,
  `command_facts` and `result_facts` are the rules, one tool_use or tool_result line at a time. A row
  is made by its tool_use id, so a resumed session that copies old lines or a batch written twice
  adds nothing. A result sets the fields of the row of the call it answers and makes none.
- `session_turns` (what a person typed), `session_said` (the last three things each session's
  assistant said) and `session_files` (the files a session edited or wrote, TR-1505). `turn_fact`,
  `said_fact` and `files_facts` are the rules, and `turns_of` folds a batch of lines into rows.

When the store takes no write, `LinesNotStored` goes up, nothing is stored and `/ingest` answers 503,
so the sender sends the same lines again. When the store is up but will not take one session's lines
(TR-1647), that session is counted and marked `lines_complete` false, its lines are not kept and one
line says so; the request is acknowledged, so one bad line blocks nothing behind it. When even the
session's counts and mark cannot be written, the batch is refused like the first case.

`sessions.lines_complete` is true when `session_lines` and the archive hold the session's
`line_count` lines. The archive moves only complete sessions. Nothing here reads `session_lines` at
start: the ten minute pass (`start_reconcile`) puts `lines_complete` right once (`repair_table_sessions`)
and keeps the gauges of the table.

A row holds the `source` of the session's first line, and the readers ask for the one source
`cc_source` names. A session whose lines come from two sources is counted under the first one that
wrote it.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import weakref
from datetime import datetime, timezone

from sqlalchemy import Text, bindparam, delete, func, insert, select, text, update

from . import metrics
from .envelope import now_utc
from .factory import is_coordination
from .session_store.tables import (session_archive_parts, session_commands, session_files,
                                   session_lines, session_messages,
                                   session_said, session_turns, sessions)

SAID_KEEP = 3                        # the assistant messages kept per session
SAID_READ = 60                       # the newest assistant lines per session a fill reads
FILES_READ = 400                     # the newest file-editing tool_use lines per session a fill reads
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
OUTPUT_TAIL = 20000                  # characters of a command's output kept: a test run's summary is at the end
MESSAGE_TEXT = 4000                  # characters of a protocol message kept
TABLE = "table"                      # `sessions.lines_origin` of a session born in session_lines (TR-1606)
CYCLE = 600.0                        # seconds between the passes of the ten minute cycle (TR-1478)
PROBE_SESSION = "tares:write-probe"  # the row a probe writes and removes in one transaction

# the rendered text names each tool as "→ name(": a cheap filter before the payload is parsed
# (the same one `Store.coordination_calls` puts in its SQL)
_COORD_HINTS = ("→ mcp__", "→ SendMessage(", "→ ListAgents(", "→ Skill(", "→ Cron")
# the same test in Python, spelled as LIKE reads it (`_` is any one character), so a line passes
# here exactly when it passes the SQL filter
_COORD_TEXT = re.compile("|".join(re.escape(h).replace("_", ".") for h in _COORD_HINTS))


def coordination_in(text: str | None, payload) -> int:
    """How many tool calls of one tool_use line only coordinate the crew (factory.is_coordination).
    The rule `Store.coordination_calls` applies: the line's text names a coordination tool, then
    each tool_use block of its payload counts when its tool does."""
    if not text or not _COORD_TEXT.search(text):
        return 0
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            return 0
    if not isinstance(payload, dict):
        return 0
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    return sum(1 for b in msg.get("content") or []
               if isinstance(b, dict) and b.get("type") == "tool_use"
               and is_coordination(b.get("name")))


def claude_code_sources(store) -> set[str]:
    """The sources session lines are stored under: `claude_code` alone (session_map.SOURCE). There
    is no source catalog that could name another (TR-1811)."""
    return {"claude_code"}


def holds_session(store, sid) -> bool:
    """False for a session id the sessions database cannot hold: Postgres takes no NUL in text, so
    such a session can never have a row there, and its lines are not kept; the rest of a batch is
    stored as usual (TR-1647, TR-1703). A posted line has lost its NULs before it gets here
    (`ClaudeCodeConnector.map_payload`)."""
    return store.sessions_db.dialect != "postgresql" or "\x00" not in str(sid)


def claude_code_lines(store, envelopes) -> list:
    """The envelopes of `envelopes` that are lines of a claude_code source, with a session."""
    cands = [e for e in envelopes if e.key_value and holds_session(store, e.key_value)]
    if not cands:
        return []
    names = {e.source for e in cands}
    if names == {"claude_code"}:
        return cands          # the default name: no catalog lookup for the common case
    ours = claude_code_sources(store)
    return [e for e in cands if e.source in ours]


def _utc(t: datetime) -> datetime:
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t


def _fold(envelopes) -> dict:
    """{session: [source, first line time, last line time, lines, tool calls, coordination calls]}"""
    out: dict = {}
    for e in envelopes:
        t = _utc(e.event_time)
        r = out.get(e.key_value)
        if r is None:
            r = out[e.key_value] = [e.source, t, t, 0, 0, 0]
        r[1], r[2] = min(r[1], t), max(r[2], t)
        r[3] += 1
        if e.event_type == "tool_use":
            r[4] += 1
            r[5] += coordination_in(e.text, e.payload)
    return out


# ── the facts the factory reads (TR-1477) ────────────────────────────────────────────────────
def _blocks(payload, requests: bool = True) -> list:
    """The content blocks of a line's payload (a JSON object, or its text), [] when there are none.
    `requests` False leaves out Claude Code's copy of a request (an older plugin recorded those as
    `api-request...` lines): its calls are the same ones the transcript lines hold."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, dict):
        return []
    if not requests and str(payload.get("type") or "").startswith("api-request"):
        return []
    msg = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    content = msg.get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(v, limit: int | None = None, tail: bool = False) -> str:
    """A string for a text column: without NUL bytes (Postgres refuses them in text), cut to `limit`
    characters from the start, or from the end when `tail`."""
    s = str(v or "").replace("\x00", "")
    if limit is None:
        return s
    return s[-limit:] if tail else s[:limit]


def _fact_id(block: dict, session: str, at: datetime, n: int) -> str:
    """A call's id: its tool_use id, or, for a block without one, a stand-in from the line."""
    return _text(block.get("id")) or f"{session}:{at.isoformat()}:{n}"


def message_facts(event_type: str, text: str | None, payload, session: str, at: datetime,
                  source: str) -> list[dict]:
    """The protocol messages of one tool_use line: its SendMessage calls whose text starts
    `[TF:<TYPE>]` (the crew's protocol, TR-429), as rows of `session_messages`."""
    if event_type != "tool_use" or not text or "SendMessage" not in text:
        return []
    out = []
    for n, b in enumerate(_blocks(payload, requests=False)):
        if not (b.get("type") == "tool_use"
                and str(b.get("name") or "").split("__")[-1] == "SendMessage"):
            continue
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        body = _text(inp.get("message") or inp.get("content"))
        m = re.match(r"\s*\[TF:([A-Z_]+)\]", body)
        if not m:
            continue
        out.append({"tool_use_id": _fact_id(b, session, at, n), "source": source,
                    "session": session, "at": at, "to_station": _text(inp.get("to")),
                    "type": m.group(1), "text": body[:MESSAGE_TEXT]})
    return out


def _codex(payload) -> dict:
    """What a line of a Codex station keeps of Codex's own (`tares.session_codex`): the command's real
    exit code, its folder and how it ended. {} for a Claude Code line."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            return {}
    own = payload.get("codex") if isinstance(payload, dict) else None
    return own if isinstance(own, dict) else {}


def command_facts(event_type: str, text: str | None, payload, session: str, at: datetime,
                  source: str) -> list[dict]:
    """The shell commands of one tool_use line: its Bash calls, as rows of `session_commands`
    (without a result yet). A Codex station's line (TR-1595) is the command's start, written before
    it ran: its row also holds the folder and the outcome `started`."""
    if event_type != "tool_use" or not text or "Bash(" not in text:
        return []
    own = _codex(payload)
    out = []
    for n, b in enumerate(_blocks(payload)):
        if b.get("type") == "tool_use" and b.get("name") == "Bash":
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            out.append({"tool_use_id": _fact_id(b, session, at, n), "source": source,
                        "session": session, "at": at, "command": _text(inp.get("command")),
                        "cwd": _text(own.get("cwd")) or None,
                        "outcome": _text(own.get("outcome")) or None})
    return out


def result_facts(event_type: str, payload, at: datetime) -> list[dict]:
    """The results of one tool_result line, by the tool_use id they answer: the tail of what was
    printed, and 1 when the tool failed (0 when it did not). A Codex station's line (TR-1595) says
    more: the real exit code (none when Codex gave none, never 0 in its place), how the command
    ended, its folder, and the command itself when its start was cut short."""
    if event_type != "tool_result":
        return []
    own = _codex(payload)
    out = []
    for b in _blocks(payload):
        tool_use_id = _text(b.get("tool_use_id"))
        if b.get("type") != "tool_result" or not tool_use_id:
            continue
        c = b.get("content")
        if isinstance(c, list):
            c = "\n".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
        row = {"tool_use_id": tool_use_id, "exit_code": 1 if b.get("is_error") else 0,
               "output_tail": _text(c, OUTPUT_TAIL, tail=True), "result_at": at,
               "cwd": None, "outcome": None, "command": None}
        if own.get("outcome"):
            code = own.get("exit_code")
            row.update(exit_code=code if isinstance(code, int) and not isinstance(code, bool) else None,
                       outcome=_text(own.get("outcome")), cwd=_text(own.get("cwd")) or None,
                       command=_text(own.get("command")) or None)
        out.append(row)
    return out


def facts_of(lines) -> dict:
    """What `lines` (rows of (source, session, time, event_type, text, payload)) hold:
    {"messages": [...], "commands": [...], "results": [...]}, each deduplicated by tool_use id
    (the first message and command of an id wins, the last result) and in id order, so two writers
    meet their rows in the same order and cannot deadlock."""
    messages: dict = {}
    commands: dict = {}
    results: dict = {}
    for source, session, at, event_type, text, payload in lines:
        at = _utc(at)
        for r in message_facts(event_type, text, payload, session, at, source):
            messages.setdefault(r["tool_use_id"], r)
        for r in command_facts(event_type, text, payload, session, at, source):
            commands.setdefault(r["tool_use_id"], r)
        for r in result_facts(event_type, payload, at):
            results[r["tool_use_id"]] = r
    return {"messages": [messages[k] for k in sorted(messages)],
            "commands": [commands[k] for k in sorted(commands)],
            "results": [results[k] for k in sorted(results)]}


def _write_facts(conn, db, facts: dict) -> None:
    """Write `facts` (from `facts_of`) in the open transaction. A message is kept once. A command
    row takes its call's fields only where it has none, so a repeat changes nothing, and a result
    sets the result fields of the call's row (commands are written first, so a call and its result
    in one batch pair)."""
    if facts["messages"]:
        conn.execute(db.upsert(session_messages).on_conflict_do_nothing(
            index_elements=["tool_use_id"]), facts["messages"])
    if facts["commands"]:
        stmt = db.upsert(session_commands)
        ex = stmt.excluded
        t = session_commands.c
        conn.execute(stmt.on_conflict_do_update(
            index_elements=["tool_use_id"],
            set_={"source": func.coalesce(t.source, ex.source),
                  "session": func.coalesce(t.session, ex.session),
                  "at": func.coalesce(t.at, ex.at),
                  "command": func.coalesce(t.command, ex.command),
                  "cwd": func.coalesce(t.cwd, ex.cwd),
                  "outcome": func.coalesce(t.outcome, ex.outcome)}), facts["commands"])
    if facts["results"]:
        # a result sets the fields of the call it answers, and makes no row of its own: it does not
        # say which tool it answers, and a row for every tool's result would hold text no reader
        # asks for. A result that comes before its call (a later batch) is written by the next fill.
        t = session_commands.c
        # A Codex station's result (TR-1595) also says how the command ended and where it ran, and gives
        # the whole command where the start held a cut one; a Claude Code result changes none of those.
        conn.execute(session_commands.update().where(t.tool_use_id == bindparam("r_id")).values(
            exit_code=bindparam("r_exit"), output_tail=bindparam("r_out"),
            result_at=bindparam("r_at"), outcome=func.coalesce(bindparam("r_outcome", type_=Text), t.outcome),
            cwd=func.coalesce(bindparam("r_cwd", type_=Text), t.cwd),
            command=func.coalesce(bindparam("r_command", type_=Text), t.command)),
            [{"r_id": r["tool_use_id"], "r_exit": r["exit_code"], "r_out": r["output_tail"],
              "r_at": r["result_at"], "r_outcome": r.get("outcome"), "r_cwd": r.get("cwd"),
              "r_command": r.get("command")} for r in facts["results"]])


# ── the turns, last words and touched files the factory reads (TR-1505) ──────────────────────
def _obj(v) -> dict:
    """A JSON object given as one or as its text; {} for anything else."""
    if isinstance(v, (str, bytes)):
        try:
            v = json.loads(v) if v else {}
        except (TypeError, ValueError):
            return {}
    return v if isinstance(v, dict) else {}


def _jx(obj: dict, *path) -> str | None:
    """SQL's json_extract_string(obj, '$.a.b') for the values these filters read: a JSON true
    or false reads as 'true' or 'false', a string as itself, a number as its text, null, a missing
    key and a step into something that is not an object as None (SQL NULL); an object or array as
    its JSON text."""
    v = obj
    for k in path:
        if not isinstance(v, dict) or k not in v:
            return None
        v = v[k]
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return json.dumps(v)
    return json.dumps(v, separators=(",", ":"), sort_keys=True)


def line_key(payload) -> str:
    """What names one line within its session: the payload's `uuid` when it has a non-empty one
    (Claude Code gives every transcript line one), else the SHA-256 of the payload's JSON."""
    o = _obj(payload)
    u = o.get("uuid")
    if isinstance(u, str) and u:
        return _text(u)
    return hashlib.sha256(json.dumps(o, sort_keys=True).encode("utf-8", "replace")).hexdigest()


def turn_fact(event_type: str, text: str | None, payload, labels, session: str, at: datetime,
              source: str) -> dict | None:
    """The row of `session_turns` one line makes, or None when the line is not a person typing: a
    `user` line that is not a subagent's (labels.sidechain), not Claude Code's own message (the
    label `meta`, the payload's isMeta, isCompactSummary, or an origin that is not human). The
    rules `FactoryStore.person_turns` read from events (TR-450, TR-634), spelled as its SQL did."""
    if event_type != "user":
        return None
    lab, pay = _obj(labels), _obj(payload)
    if ((_jx(lab, "sidechain") or "false") == "true" or (_jx(lab, "meta") or "false") == "true"
            or (_jx(pay, "isMeta") or "false") == "true"
            or (_jx(pay, "isCompactSummary") or "false") == "true"
            or (_jx(pay, "origin", "kind") or "human") != "human"):
        return None
    who = _jx(lab, "tares_person")
    return {"session": session, "line_key": line_key(pay), "source": source, "at": at,
            "person": None if who is None else _text(who), "text": _text(text)}


def said_fact(event_type: str, text: str | None, payload, session: str, at: datetime,
              source: str) -> dict | None:
    """The row of `session_said` one line makes, or None: an `assistant` line whose text, stripped,
    is not empty and does not start with the arrow that marks a tool call. The text is kept
    stripped."""
    if event_type != "assistant":
        return None
    t = _text(text).strip()
    if not t or t.startswith("→"):
        return None
    return {"session": session, "line_key": line_key(payload), "source": source, "at": at,
            "text": t}


def files_facts(event_type: str, text: str | None, payload, session: str, at: datetime,
                source: str) -> list[dict]:
    """The files one tool_use line edited or wrote, as rows of `session_files`: each tool_use block
    named Edit, Write, MultiEdit or NotebookEdit, by its input's file_path or notebook_path. The
    line's text names each call as "→ name(", so a line whose text holds neither "Edit(" nor
    "Write(" is passed over before its payload is parsed (MultiEdit( and NotebookEdit( hold
    "Edit(")."""
    if event_type != "tool_use" or not text or ("Edit(" not in text and "Write(" not in text):
        return []
    out = []
    for b in _blocks(payload):
        if b.get("type") != "tool_use" or str(b.get("name") or "") not in EDIT_TOOLS:
            continue
        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
        path = _text(inp.get("file_path") or inp.get("notebook_path"))
        if path:
            out.append({"session": session, "path": path, "source": source, "last_at": at})
    return out


def turns_of(lines) -> dict:
    """What `lines` (rows of (source, session, time, event_type, text, payload, labels)) hold:
    {"turns": [...], "said": [...], "files": [...]}. A turn and a said line are kept once by
    (session, line key) (the first wins), a file once by (session, path) with its latest time, and
    each list is in key order, so two writers meet their rows in the same order and cannot
    deadlock."""
    turns: dict = {}
    said: dict = {}
    files: dict = {}
    for source, session, at, event_type, text, payload, labels in lines:
        if event_type not in ("user", "assistant", "tool_use"):
            continue
        at = _utc(at)
        r = turn_fact(event_type, text, payload, labels, session, at, source)
        if r:
            turns.setdefault((r["session"], r["line_key"]), r)
        r = said_fact(event_type, text, payload, session, at, source)
        if r:
            said.setdefault((r["session"], r["line_key"]), r)
        for r in files_facts(event_type, text, payload, session, at, source):
            k = (r["session"], r["path"])
            if k not in files or files[k]["last_at"] < r["last_at"]:
                files[k] = r
    return {"turns": [turns[k] for k in sorted(turns)], "said": [said[k] for k in sorted(said)],
            "files": [files[k] for k in sorted(files)]}


def _write_turns(conn, db, rows: dict) -> None:
    """Write `rows` (from `turns_of`) in the open transaction. A turn and a said line are written
    once; a file keeps the greatest time. Then each touched session's said rows are cut to the
    newest SAID_KEEP, in the same transaction, so a reader never sees more."""
    if rows["turns"]:
        conn.execute(db.upsert(session_turns).on_conflict_do_nothing(
            index_elements=["session", "line_key"]), rows["turns"])
    if rows["said"]:
        conn.execute(db.upsert(session_said).on_conflict_do_nothing(
            index_elements=["session", "line_key"]), rows["said"])
        sids = sorted({r["session"] for r in rows["said"]})
        t = session_said.c
        held: dict = {}
        for i in range(0, len(sids), 500):
            for sid, key, at in conn.execute(select(t.session, t.line_key, t.at).where(
                    t.session.in_(sids[i:i + 500]))):
                held.setdefault(sid, []).append((at, key))
        drop = []
        for sid in sorted(held):
            for _, key in sorted(held[sid], reverse=True)[SAID_KEEP:]:
                drop.append({"d_session": sid, "d_key": key})
        if drop:
            conn.execute(delete(session_said).where(
                t.session == bindparam("d_session"), t.line_key == bindparam("d_key")), drop)
    if rows["files"]:
        stmt = db.upsert(session_files)
        stmt = stmt.on_conflict_do_update(
            index_elements=["session", "path"],
            set_={"last_at": db.greatest(session_files.c.last_at, stmt.excluded.last_at)})
        conn.execute(stmt, rows["files"])


def _add_turns(store, rows: dict) -> None:
    """Write a batch's turns, last words and files in a transaction of their own: a line they
    cannot take must not lose the batch's counts or facts."""
    if not any(rows.values()):
        return
    db = store.sessions_db
    with db.begin() as conn:
        _write_turns(conn, db, rows)


def _add(store, folded: dict, lines: dict | None = None, also=None) -> dict:
    """Add each session's counts to its row, in one transaction. Rows are written in session order,
    so two writers on Postgres meet their rows in the same order and cannot deadlock. With `lines`
    ({session: [row of session_lines without its seq, ...]}, from `_line_rows`) the same
    transaction numbers each session's lines from the `line_count` the upsert returns, and inserts
    them: a line is counted and stored together or not at all, and two batches of one session never
    take the same `seq`. `also(conn)` writes what must land in that transaction too. Returns
    {session: line_count after the write} when `lines` was given, else {}."""
    db = store.sessions_db
    now = now_utc()
    stmt = db.upsert(sessions)
    stmt = stmt.on_conflict_do_update(
        index_elements=["session"],
        set_={"line_count": sessions.c.line_count + stmt.excluded.line_count,
              "tool_calls": sessions.c.tool_calls + stmt.excluded.tool_calls,
              "coordination_calls": (sessions.c.coordination_calls
                                     + stmt.excluded.coordination_calls),
              "started_at": db.least(sessions.c.started_at, stmt.excluded.started_at),
              "last_line_at": db.greatest(sessions.c.last_line_at, stmt.excluded.last_line_at),
              "updated_at": stmt.excluded.updated_at})
    # a session with no row yet is born in the table (TR-1606): complete when its lines go in with
    # its counts, short when they do not
    rows = [{"session": sid, "source": r[0], "started_at": r[1], "last_line_at": r[2],
             "line_count": r[3], "tool_calls": r[4], "coordination_calls": r[5], "updated_at": now,
             "lines_origin": "table", "lines_complete": bool(lines)}
            for sid, r in sorted(folded.items())]
    with db.begin() as conn:
        if not lines:
            conn.execute(stmt, rows)
            if also:
                also(conn)
            return {}
        c = sessions.c
        total = dict(conn.execute(stmt.returning(c.session, c.line_count), rows).all())
        out = []
        for sid in sorted(lines):
            mine = lines[sid]
            first = total[sid] - len(mine) + 1
            out += [{**r, "session": sid, "seq": first + i} for i, r in enumerate(mine)]
        conn.execute(insert(session_lines), out)
        if also:
            also(conn)
    return total


def render_flags(store, sources) -> dict:
    """{source: the `render` text of `session_lines`} for the sources of a batch: the flags the
    runtime intake builds a line's text with (`include_thinking`, `redact`), its defaults for
    every source (TR-1804; no source catalog holds other flags, TR-1811)."""
    flags = {"include_thinking": False, "redact": True}
    return {s: json.dumps(flags, sort_keys=True) for s in sources}


def _line_rows(store, envelopes) -> dict:
    """{session: [the rows of `session_lines` the session's lines make, in batch order]}, without
    `seq` (`_add` numbers them). `payload` and `labels` are the line's JSON text."""
    render = render_flags(store, {e.source for e in envelopes})
    out: dict = {}
    for e in envelopes:
        out.setdefault(e.key_value, []).append({
            "line_key": _text(line_key(e.payload)), "at": _utc(e.event_time),
            "type": _text(e.event_type), "labels": json.dumps(e.labels),
            "render": render[e.source], "payload": json.dumps(e.payload)})
    return out


def _add_facts(store, facts: dict) -> None:
    """Write a batch's facts in a transaction of their own: a line the facts cannot take must not
    lose the batch's session counts."""
    if not any(facts.values()):
        return
    db = store.sessions_db
    with db.begin() as conn:
        _write_facts(conn, db, facts)


# ── the session store is written first (TR-1665) ──────────────────────────────────────────────
# A claude_code line is not acknowledged until the store has it, or has a durable mark that says it
# is missing. `Store.append` calls `record_first`.
class LinesNotStored(RuntimeError):
    """The sessions database takes no write: the lines are not acknowledged, and nothing of them is
    stored, so the sender sends them again."""


class Written:
    """What `record_first` wrote for one batch: `counted` is [(session, lines, tool calls,
    coordination calls, the last seq of its rows or None when no rows were written)], what
    `take_back` undoes; `refused` the sessions whose lines the store would not take."""

    def __init__(self):
        self.counted: list = []
        self.refused: set = set()

    def add(self, folded: dict, totals: dict) -> None:
        self.counted += [(sid, r[3], r[4], r[5], totals.get(sid)) for sid, r in sorted(folded.items())]


def _probe(store) -> None:
    """Raise `LinesNotStored` unless the store takes a plain write now: a harmless row of
    `sessions`, written and removed in one transaction. It tells a store that is down from one that
    refuses a particular line."""
    now = now_utc()
    try:
        with store.sessions_db.begin() as conn:
            conn.execute(insert(sessions).values(
                session=PROBE_SESSION, source="tares", started_at=now, last_line_at=now,
                line_count=0, tool_calls=0, coordination_calls=0, updated_at=now,
                lines_origin=TABLE, lines_complete=True))
            conn.execute(delete(sessions).where(sessions.c.session == PROBE_SESSION))
    except Exception as e:
        raise LinesNotStored(f"the sessions database does not take writes ({type(e).__name__}: "
                               f"{e}); send the lines again") from e


def _write_whole(store, envelopes, written: Written) -> None:
    """Counts, lines, facts and turns of `envelopes` in one transaction: all of it or nothing."""
    db = store.sessions_db
    folded = _fold(envelopes)
    lines = _line_rows(store, envelopes)
    facts = facts_of((e.source, e.key_value, e.event_time, e.event_type, e.text, e.payload)
                     for e in envelopes)
    turns = turns_of((e.source, e.key_value, e.event_time, e.event_type, e.text, e.payload, e.labels)
                     for e in envelopes)

    def also(conn):
        _write_facts(conn, db, facts)
        _write_turns(conn, db, turns)
    written.add(folded, _add(store, folded, lines, also))


def _write_refused(store, sid: str, envelopes, written: Written) -> None:
    """A session whose lines the store will not take: its counts, with the durable mark that its
    lines are missing (`lines_complete` false), in one transaction; then its facts and turns, as far
    as they go. The lines are not kept.

    When that transaction does not go through either, nothing would ever say the lines are missing,
    so they are not acknowledged: `LinesNotStored`, and the sender sends them again (TR-1647)."""
    folded = _fold(envelopes)

    def mark(conn):
        conn.execute(update(sessions).where(sessions.c.session == sid).values(lines_complete=False))
    written.refused.add(sid)
    for last_try in (False, True):
        try:
            _add(store, folded, also=mark)
            written.add(folded, {})
            break
        except Exception as e:
            _probe(store)          # down: the whole batch is refused and sent again
            if last_try:           # up, and still neither counted nor marked
                print(f"taresd: the sessions database does not take the row of session {sid} "
                      f"({type(e).__name__}: {e}); {len(envelopes)} claude_code line(s) are not "
                      "acknowledged, the sender sends them again", flush=True)
                raise LinesNotStored(f"the sessions database does not take the row of session "
                                     f"{sid} ({type(e).__name__}: {e}); send the lines again") from e
    for what, write in (
            ("facts", lambda: _add_facts(store, facts_of(
                (e.source, e.key_value, e.event_time, e.event_type, e.text, e.payload)
                for e in envelopes))),
            ("turns", lambda: _add_turns(store, turns_of(
                (e.source, e.key_value, e.event_time, e.event_type, e.text, e.payload, e.labels)
                for e in envelopes)))):
        try:
            write()
        except Exception as e:
            print(f"taresd: writing the {what} of {len(envelopes)} claude_code line(s) failed "
                  f"({type(e).__name__}: {e})", flush=True)


def record_first(store, envelopes) -> Written:
    """Write a batch of claude_code lines to the session store.

    The batch goes in one transaction. When that fails: a store that takes no write raises
    `LinesNotStored` and nothing is stored. Otherwise each session is tried again on its own (a
    passing error succeeds then), and one that still fails is counted and marked incomplete, and its
    lines are not kept, so a line that can never be stored does not make the sender send it for
    ever. Called under `Store._sess_lock`."""
    written = Written()
    if not envelopes:
        return written
    t0 = time.perf_counter()
    try:
        _write_whole(store, envelopes, written)
    except Exception:
        by_session: dict = {}
        for e in envelopes:
            by_session.setdefault(e.key_value, []).append(e)
        try:
            _probe(store)
            for sid, mine in sorted(by_session.items()):
                try:
                    _write_whole(store, mine, written)
                except Exception as e:
                    _probe(store)
                    why = f"{type(e).__name__}: {e}"
                    _write_refused(store, sid, mine, written)
                    print(f"taresd: the sessions database refused {len(mine)} claude_code line(s) "
                          f"of session {sid} ({why}); counted and marked incomplete, the lines "
                          "are not kept", flush=True)
        except LinesNotStored:
            take_back(store, written)
            metrics.session_ingest(time.perf_counter() - t0, False)
            raise
    metrics.session_ingest(time.perf_counter() - t0, not written.refused)
    return written


def take_back(store, written: Written) -> None:
    """Undo the counts and lines `record_first` wrote for a batch that is refused after all, so the
    batch sent again is not counted twice. Facts and turns need no undoing: a second write of them
    changes nothing. Called under the same hold of `Store._sess_lock`."""
    if not written.counted:
        return
    c = sessions.c
    try:
        with store.sessions_db.begin() as conn:
            for sid, n, tools, coord, last in sorted(written.counted, key=lambda r: r[0]):
                if last is not None:
                    conn.execute(delete(session_lines).where(
                        session_lines.c.session == sid, session_lines.c.seq > last - n))
                conn.execute(update(sessions).where(c.session == sid).values(
                    line_count=c.line_count - n, tool_calls=c.tool_calls - tools,
                    coordination_calls=c.coordination_calls - coord))
                conn.execute(delete(sessions).where(c.session == sid, c.line_count <= 0))
    except Exception as e:
        print(f"taresd: {sum(r[1] for r in written.counted)} claude_code line(s) stayed in the "
              f"sessions database after their batch was refused ({type(e).__name__}: {e}); sent "
              "again, they are stored there twice", flush=True)


def _held_lines():
    """The count of the lines `session_lines` and the archive hold for `sessions.session`: rows of
    the table plus the `lines` of the archived parts (TR-1506). One statement with the session row,
    so one snapshot on Postgres: a session the archive moves between two queries (rows deleted, part
    inserted, one transaction) would be counted twice or not at all."""
    kept = select(func.count()).where(session_lines.c.session == sessions.c.session).scalar_subquery()
    archived = select(func.coalesce(func.sum(session_archive_parts.c.lines), 0)).where(
        session_archive_parts.c.session == sessions.c.session).scalar_subquery()
    return kept + archived


def repair_table_sessions(store) -> int:
    """Recompute `lines_complete` of every session born in the table (TR-1606): complete when the
    table and the archive hold `line_count` lines. Run once per process, on the ten minute cycle's
    first pass, never at start: it puts right a session an older version marked against another
    count, or that a failed write left false. Two statements, one per direction, that touch only the
    rows that change. Returns how many rows changed."""
    whole = _held_lines() == sessions.c.line_count
    n = 0
    with store.sessions_db.begin() as conn:
        for flag in (True, False):
            n += conn.execute(update(sessions).where(
                sessions.c.lines_origin == TABLE, sessions.c.lines_complete.is_(not flag),
                whole if flag else ~whole).values(lines_complete=flag)).rowcount
    return n


def lines_size(store) -> tuple[int, int]:
    """(rows, bytes) of `session_lines`, for the gauges: the row count, and the table's size with
    its indexes on Postgres (`pg_total_relation_size`) or the whole file's size on SQLite (pages
    times page size: SQLite has no size per table without a build option)."""
    db = store.sessions_db
    with db.read() as conn:
        rows = conn.execute(select(func.count()).select_from(session_lines)).scalar_one()
        if db.dialect == "postgresql":
            size = conn.execute(text("SELECT pg_total_relation_size('session_lines')")).scalar_one()
        else:
            size = (conn.execute(text("PRAGMA page_count")).scalar_one()
                    * conn.execute(text("PRAGMA page_size")).scalar_one())
    return int(rows), int(size)


def refresh_lines_gauges(store) -> None:
    """Set `tares_session_lines_rows` and `tares_session_lines_bytes`; a failure leaves them as
    they were and prints nothing (a gauge must never be the news)."""
    try:
        metrics.session_lines_size(*lines_size(store))
    except Exception:
        pass


def _repair_pass(store) -> None:
    try:
        n = repair_table_sessions(store)
        if n:
            print(f"taresd: lines_complete put right for {n} session(s)", flush=True)
    except Exception as e:
        print(f"taresd: lines_complete repair failed ({type(e).__name__}: {e}); "
              "tried again at the next start", flush=True)


def _cycle(ref, stop: threading.Event) -> None:
    """Every `CYCLE` seconds: the gauges of `session_lines`, the hourly usage totals and, on the
    first pass only, the `lines_complete` repair. Ends when `Store.close` sets `stop`, or when the
    store is gone: the thread holds a weak reference, so a process that exits without closing its
    store (the daemon) is not kept from freeing it by a thread parked in `stop.wait`."""
    repaired = False
    while not stop.wait(CYCLE):
        store = ref()
        if store is None:
            return
        if not repaired:
            _repair_pass(store)
            repaired = True
        refresh_lines_gauges(store)
        _usage_hourly_pass(store)       # a no-op once the hourly totals are built (TR-1632)
        del store


def _usage_hourly_pass(store) -> None:
    """Build the hourly usage totals the page reads (`claude_usage_hourly.fill`): chunked, resumes
    from its cursor, sets its ready mark only after the totals match the usage rows."""
    from . import claude_usage_hourly
    t = time.perf_counter()
    try:
        n = claude_usage_hourly.fill(store)
        if n:
            print(f"taresd: hourly usage totals built for {n} session(s) in "
                  f"{time.perf_counter() - t:.1f}s", flush=True)
    except Exception as e:
        print(f"taresd: hourly usage totals failed ({type(e).__name__}: {e}); "
              "tried again at the next pass", flush=True)


def start_reconcile(store) -> threading.Thread:
    """Build the hourly usage totals in a background thread, once (the thread returned), and run
    the ten minute cycle (`_cycle`) in another. Nothing here reads `session_lines` at start. A
    failure prints one line: the next pass tries again."""
    th = threading.Thread(target=_usage_hourly_pass, args=(store,), name="sessions-reconcile",
                          daemon=True)
    th.start()
    store._reconcile_thread = th    # `Store.close` waits for it before the connections go
    stop = getattr(store, "_reconcile_stop", None)
    if stop is None:
        stop = store._reconcile_stop = threading.Event()
        threading.Thread(target=_cycle, args=(weakref.ref(store), stop), name="sessions-reconcile-cycle",
                         daemon=True).start()
    return th
