"""What each Claude Code session's lines add up to, kept in the `sessions` table (P-TR-295, TR-1471).

A page that lists sessions used to count over the events table, and that table grows with every
line. Here each line is counted once, as it is stored, into one row per session. The readers
`Store.session_activity`, `Store.project_sessions`, `FactoryStore.station_sessions` and
`FactoryStore.coordination_calls` read the row (TR-1499) once the table has caught up (see
`caught_up`), and scan events until then (TR-1520), so a cell just upgraded shows right numbers
at once. Two writers keep the row right:

- `record`, called by `Store.append` after a batch of claude_code lines is committed to DuckDB. It
  adds the batch to each session's row in one transaction (`ON CONFLICT ... DO UPDATE`), so
  concurrent batches lose nothing.
- `reconcile_sessions`, run in a background thread at start. It reads the absolute counts from
  DuckDB for the sessions with lines newer than the last pass, and keeps the larger of what is
  stored and what DuckDB says. Lines written by an image that did not call `record`, or a batch
  whose `record` failed, are counted this way. Both writers only ever undercount, so the larger
  value is the right one, and a second pass changes nothing. When a pass has finished all its
  chunks it sets the `sessions_caught_up` row in `session_ingest_marks`; a pass that raised does
  not, so the readers stay on the scan and the next start tries again.

The readers know the table has caught up from one row in `session_ingest_marks`, `CAUGHT_UP`. It
is set by the first complete `reconcile_sessions`, or at once by `Store.__init__` on a cell whose
events hold no claude_code line (nothing to catch up on). It is never unset.

DuckDB is never changed: `Store.append` keeps writing every line there.

The same two writers keep the facts the factory reads (TR-1477): the crew's protocol messages in
`session_messages` and the shell commands sessions ran, with what they printed, in
`session_commands`. `message_facts`, `command_facts` and `result_facts` are the extraction rules, one
tool_use or tool_result line at a time; `record` writes what they find in a transaction after the
counts' (so a line the facts cannot take never loses a batch's counts), and `fill_facts` runs them over the events a first start has not seen (the watermark
`session_facts`). A row is made by its tool_use id, so a resumed session that copies old lines, a
batch written twice or a fill that runs twice adds nothing. A result sets the fields of the row of
the call it answers and makes none: a result that comes before its call is written by the next fill.

The same two writers keep three more tables the factory reads (TR-1505): `session_turns` (what a
person typed), `session_said` (the last three things each session's assistant said) and
`session_files` (the files a session edited or wrote, with when it last did). `turn_fact`,
`said_fact` and `files_facts` are the rules, one line at a time, and `turns_of` folds a batch of
lines into rows. `record` writes them in a third transaction of its own, after the facts, so a
failure there loses neither the counts nor the facts. `fill_turns` reads them back from DuckDB for
the lines a first start has not seen (the watermark `session_turns`): every user line newer than
the mark, the 60 newest assistant lines and the 400 newest file-editing tool_use lines of each
session touched. A turn and a said line are made by (session, line key), a file by (session, path),
so a repeat adds nothing. The readers `FactoryStore.person_turns`, `Store.said_lately`,
`Store.last_said` and `Store.files_touched` read these tables and no longer scan events.

The same writers keep `session_lines` (TR-1478): every claude_code line as it was stored, numbered
within its session by `seq`. `record` takes `seq` from the `line_count` the `sessions` upsert returns
and inserts the lines in that transaction, so `seq` is dense and unique per session on SQLite and
Postgres. DuckDB does not deduplicate a line, so `session_lines` does not either, and the two counts
can be compared: `sessions.lines_complete` is true when a session has as many rows in
`session_lines` as lines in DuckDB. A new row starts false; the reconcile sets it for the sessions
it reads (every 10 minutes, and at start), and a `record` that could not write a session's lines
sets it false. The hourly archive (`session_archive.py`, TR-1506) moves the rows of sessions idle for
a week into files and records each file in `session_archive_parts`; the count compared with DuckDB's
is the rows of `session_lines` plus the `lines` of the parts. `session_read.py` (TR-1507) reads a
complete session from the table and the parts instead of DuckDB; nothing fills the table from old events (milestone 2). `tares_session_lines_rows` and `tares_session_lines_bytes` say how big it is.

Two things to know when reading the table:

- A row holds the `source` of the session's first line, and the readers ask for the one source
  `cc_source` names, as the events scan did. A session whose lines come from two sources is
  counted under the first one that wrote it.
- The reconcile's mark is the newest `ingest_time` it read, and a line's `ingest_time` is set when
  its envelope is built, not when it is committed. The 10 minute margin before the mark covers a batch
  that commits late.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import weakref
from datetime import datetime, timedelta, timezone

from sqlalchemy import Text, bindparam, delete, func, insert, select, text, update

from . import metrics
from .envelope import now_utc
from .factory import is_coordination
from .session_store.tables import (session_archive_parts, session_commands, session_files,
                                   session_ingest_marks, session_lines, session_messages,
                                   session_said, session_turns, sessions)

MARK = "sessions_reconcile"          # the fill's row in session_ingest_marks
CAUGHT_UP = "sessions_caught_up"     # its row when a pass has finished (TR-1520): readers use the table
MARGIN = timedelta(minutes=10)       # a pass starts this long before the last one's newest line
CHUNK = 200                          # sessions read from DuckDB under one hold of the lock
FACTS_MARK = "session_facts"         # the facts fill's row in session_ingest_marks (TR-1477)
TURNS_MARK = "session_turns"         # the turns fill's row in session_ingest_marks (TR-1505)
SAID_KEEP = 3                        # the assistant messages kept per session
SAID_READ = 60                       # the newest assistant lines per session a fill reads
FILES_READ = 400                     # the newest file-editing tool_use lines per session a fill reads
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
OUTPUT_TAIL = 20000                  # characters of a command's output kept: a test run's summary is at the end
MESSAGE_TEXT = 4000                  # characters of a protocol message kept
CYCLE = 600.0                        # seconds between the reconcile passes after the first (TR-1478)

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
    """The names of the cell's claude_code sources (`claude_code` too, which `cc_source` falls back
    to when the catalog has none)."""
    with store._sread:
        rows = store.sc.execute(
            "SELECT name FROM catalog_sources WHERE connector = 'claude_code'").fetchall()
    return {r[0] for r in rows} | {"claude_code"}


def holds_session(store, sid) -> bool:
    """False for a session id the sessions database cannot hold: Postgres takes no NUL in text, so
    such a session can never have a row there. DuckDB alone keeps its lines, and the rest of a batch
    or of a fill is stored as usual (TR-1647, TR-1703). A posted line has lost its NULs before it
    gets here (`ClaudeCodeConnector.map_payload`); DuckDB may hold ids from before that."""
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
    return str(block["id"]) if block.get("id") else f"{session}:{at.isoformat()}:{n}"


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
    """What a line of a Codex station keeps of Codex's own (`tares.connectors.codex`): the command's real
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
        if b.get("type") != "tool_result" or not b.get("tool_use_id"):
            continue
        c = b.get("content")
        if isinstance(c, list):
            c = "\n".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
        row = {"tool_use_id": str(b["tool_use_id"]), "exit_code": 1 if b.get("is_error") else 0,
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
    """A JSON object given as one (at ingest) or as its text (from DuckDB); {} for anything else."""
    if isinstance(v, (str, bytes)):
        try:
            v = json.loads(v) if v else {}
        except (TypeError, ValueError):
            return {}
    return v if isinstance(v, dict) else {}


def _jx(obj: dict, *path) -> str | None:
    """DuckDB's json_extract_string(obj, '$.a.b') for the values these filters read: a JSON true
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


def _add(store, folded: dict, lines: dict | None = None) -> None:
    """Add each session's counts to its row, in one transaction. Rows are written in session order,
    so two writers on Postgres meet their rows in the same order and cannot deadlock. With `lines`
    ({session: [row of session_lines without its seq, ...]}, from `_line_rows`) the same
    transaction numbers each session's lines from the `line_count` the upsert returns, and inserts
    them: a line is counted and stored together or not at all, and two batches of one session never
    take the same `seq`."""
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
    rows = [{"session": sid, "source": r[0], "started_at": r[1], "last_line_at": r[2],
             "line_count": r[3], "tool_calls": r[4], "coordination_calls": r[5], "updated_at": now}
            for sid, r in sorted(folded.items())]
    with db.begin() as conn:
        if not lines:
            conn.execute(stmt, rows)
            return
        c = sessions.c
        total = dict(conn.execute(stmt.returning(c.session, c.line_count), rows).all())
        out = []
        for sid in sorted(lines):
            mine = lines[sid]
            first = total[sid] - len(mine) + 1
            out += [{**r, "session": sid, "seq": first + i} for i, r in enumerate(mine)]
        conn.execute(insert(session_lines), out)


def render_flags(store, sources) -> dict:
    """{source: the `render` text of `session_lines`} for the claude_code sources of a batch: the
    flags the connector builds a line's text with (`include_thinking`, `redact`), read from the
    source's config in the catalog, with the connector's defaults when the source has none."""
    flags = {s: {"include_thinking": False, "redact": True} for s in sources}
    with store._sread:
        rows = store.sc.execute(
            "SELECT name, config FROM catalog_sources WHERE connector = 'claude_code'").fetchall()
    for name, raw in rows:
        if name not in flags:
            continue
        try:
            cfg = json.loads(raw or "{}")
        except (TypeError, ValueError):
            cfg = {}
        cfg = cfg if isinstance(cfg, dict) else {}
        flags[name] = {"include_thinking": bool(cfg.get("include_thinking", False)),
                       "redact": bool(cfg.get("redact", True))}
    return {s: json.dumps(f, sort_keys=True) for s, f in flags.items()}


def _line_rows(store, envelopes) -> dict:
    """{session: [the rows of `session_lines` the session's lines make, in batch order]}, without
    `seq` (`_add` numbers them). `payload` and `labels` are the text DuckDB stores for the line
    (`Store._append` dumps them the same way), so the table holds what the events table holds."""
    render = render_flags(store, {e.source for e in envelopes})
    out: dict = {}
    for e in envelopes:
        out.setdefault(e.key_value, []).append({
            "line_key": _text(line_key(e.payload)), "at": _utc(e.event_time),
            "type": _text(e.event_type), "labels": json.dumps(e.labels),
            "render": render[e.source], "payload": json.dumps(e.payload)})
    return out


def _lines_incomplete(store, sids) -> None:
    """Say a session's lines are not all in `session_lines`: set `lines_complete` false for each
    session (one that has no row yet starts false)."""
    sids = sorted(sids)
    db = store.sessions_db
    with db.begin() as conn:
        for i in range(0, len(sids), 500):
            conn.execute(update(sessions).where(sessions.c.session.in_(sids[i:i + 500]))
                         .values(lines_complete=False))


def _add_facts(store, facts: dict) -> None:
    """Write a batch's facts in a transaction of their own: a line the facts cannot take must not
    lose the batch's session counts."""
    if not any(facts.values()):
        return
    db = store.sessions_db
    with db.begin() as conn:
        _write_facts(conn, db, facts)


def record(store, envelopes) -> None:
    """Count a batch of claude_code lines (already committed to DuckDB) into `sessions`. A failure
    logs one line and counts in tares_session_ingest_failures_total; it never reaches the caller,
    because the lines are stored and the next start's reconcile counts them."""
    if not envelopes:
        return
    t0 = time.perf_counter()
    ok = True
    folded: dict = {}
    try:
        folded = _fold(envelopes)
        _add(store, folded, _line_rows(store, envelopes))
    except Exception as e:
        ok = False
        print(f"taresd: counting {len(envelopes)} claude_code line(s) into sessions failed "
              f"({type(e).__name__}: {e}); caught up at the next start", flush=True)
        # the counts must not be lost to a line `session_lines` cannot take: count again without
        # the lines, and say their sessions are missing some
        try:
            _add(store, folded)
        except Exception:
            pass
        try:
            _lines_incomplete(store, folded)
        except Exception:
            pass
    try:
        _add_facts(store, facts_of(
            (e.source, e.key_value, e.event_time, e.event_type, e.text, e.payload)
            for e in envelopes))
    except Exception as e:
        ok = False
        print(f"taresd: writing the facts of {len(envelopes)} claude_code line(s) failed "
              f"({type(e).__name__}: {e}); written at the next start", flush=True)
    try:
        _add_turns(store, turns_of(
            (e.source, e.key_value, e.event_time, e.event_type, e.text, e.payload, e.labels)
            for e in envelopes))
    except Exception as e:
        ok = False
        print(f"taresd: writing the turns of {len(envelopes)} claude_code line(s) failed "
              f"({type(e).__name__}: {e}); written at the next start", flush=True)
    metrics.session_ingest(time.perf_counter() - t0, ok)


# ── catching up from DuckDB ──────────────────────────────────────────────────────────────────
def _mark(store):
    with store.sessions_db.begin() as conn:
        return conn.execute(select(session_ingest_marks.c.at).where(
            session_ingest_marks.c.name == MARK)).scalar_one_or_none()


def _set_mark(store, at: datetime) -> None:
    db = store.sessions_db
    stmt = db.upsert(session_ingest_marks).values(name=MARK, at=at)
    stmt = stmt.on_conflict_do_update(index_elements=["name"], set_={"at": stmt.excluded.at})
    with db.begin() as conn:
        conn.execute(stmt)


def caught_up(store) -> bool:
    """True once the `sessions` table holds every session's counts: the first complete reconcile
    ran, or the cell had no claude_code line to count. One primary-key lookup. Only a True answer
    is kept on the store (the row is never unset, and losing the answer changes nothing)."""
    if getattr(store, "_sessions_caught_up", False):
        return True
    with store.sessions_db.begin() as conn:
        at = conn.execute(select(session_ingest_marks.c.at).where(
            session_ingest_marks.c.name == CAUGHT_UP)).first()
    if at is None:
        return False
    store._sessions_caught_up = True
    return True


def _set_caught_up(store) -> None:
    db = store.sessions_db
    stmt = db.upsert(session_ingest_marks).values(name=CAUGHT_UP, at=now_utc())
    stmt = stmt.on_conflict_do_nothing(index_elements=["name"])
    with db.begin() as conn:
        conn.execute(stmt)
    store._sessions_caught_up = True


def mark_caught_up_if_empty(store) -> bool:
    """The one-time shortcut at `Store.__init__`: a cell with no caught-up row whose DuckDB events
    hold no claude_code line has nothing to catch up on, so it counts as caught up at once. A cell
    that has such lines stays on the scan until its reconcile finishes. Returns the state."""
    if caught_up(store):
        return True
    sources = sorted(claude_code_sources(store))
    marks = ", ".join("?" for _ in sources)
    with store._lock:
        has = store.con.execute(
            f"SELECT 1 FROM events WHERE source IN ({marks}) LIMIT 1", sources).fetchone()
    if has is not None:
        return False
    _set_caught_up(store)
    return True


def reconcile_sessions(store, chunk: int = CHUNK) -> int:
    """Bring `sessions` up to what DuckDB holds, for the sessions with lines newer than the last
    pass (every session on the first pass). Each counter becomes the larger of the stored value and
    the one DuckDB computes; the mark then moves to the newest `ingest_time` read. Reads DuckDB in
    chunks of `chunk` sessions, taking and letting go of the store's lock for each, and writes
    under the same lock that `Store.append` holds across its commit and `record`, so a line is
    never counted by both. Sets the caught-up row when every chunk has been written (even when it
    found no session). Returns how many sessions it looked at."""
    sources = sorted(claude_code_sources(store))
    marks = ", ".join("?" for _ in sources)
    mark = _mark(store)
    since = mark - MARGIN if mark is not None else None
    with store._lock:
        found = store.con.execute(
            f"SELECT key_value, max(ingest_time) FROM events WHERE source IN ({marks}) "
            "AND key_value IS NOT NULL AND key_value <> '' "
            "AND (CAST(? AS TIMESTAMPTZ) IS NULL OR ingest_time >= ?) GROUP BY key_value",
            [*sources, since, since]).fetchall()
    newest = max((r[1] for r in found), default=None)
    sids = sorted(r[0] for r in found if holds_session(store, r[0]))
    for i in range(0, len(sids), chunk):
        _reconcile_chunk(store, sources, sids[i:i + chunk])
    if newest is not None:
        _set_mark(store, _utc(newest))
    _set_caught_up(store)          # only here: a pass that raised above leaves the readers on the scan
    return len(sids)


def _reconcile_chunk(store, sources: list, sids: list) -> None:
    marks = ", ".join("?" for _ in sources)
    keys = ", ".join("?" for _ in sids)
    args = [*sources, *sids]
    with store._sess_lock:
        with store._lock:
            counts = store.con.execute(
                "SELECT key_value, any_value(source), min(event_time), max(event_time), count(*), "
                "count(*) FILTER (WHERE event_type = 'tool_use') FROM events "
                f"WHERE source IN ({marks}) AND key_value IN ({keys}) GROUP BY key_value",
                args).fetchall()
            hints = " OR ".join("text LIKE ?" for _ in _COORD_HINTS)
            lines = store.con.execute(
                "SELECT key_value, text, payload FROM events "
                f"WHERE source IN ({marks}) AND key_value IN ({keys}) AND event_type = 'tool_use' "
                f"AND ({hints})", [*args, *[f"%{h}%" for h in _COORD_HINTS]]).fetchall()
        coord: dict = {}
        for sid, text, payload in lines:
            n = coordination_in(text, payload)
            if n:
                coord[sid] = coord.get(sid, 0) + n
        computed = {sid: (src, _utc(first), _utc(last), int(n), int(tools), coord.get(sid, 0))
                    for sid, src, first, last, n, tools in counts}
        if not computed:
            return
        db = store.sessions_db
        now = now_utc()
        stmt = db.upsert(sessions)
        ex = stmt.excluded
        # the larger of what is stored and what DuckDB says: either one can only undercount
        stmt = stmt.on_conflict_do_update(
            index_elements=["session"],
            set_={"line_count": db.greatest(sessions.c.line_count, ex.line_count),
                  "tool_calls": db.greatest(sessions.c.tool_calls, ex.tool_calls),
                  "coordination_calls": db.greatest(sessions.c.coordination_calls,
                                                    ex.coordination_calls),
                  "started_at": db.least(sessions.c.started_at, ex.started_at),
                  "last_line_at": db.greatest(sessions.c.last_line_at, ex.last_line_at),
                  "updated_at": ex.updated_at})
        rows = [{"session": sid, "source": c[0], "started_at": c[1], "last_line_at": c[2],
                 "line_count": c[3], "tool_calls": c[4], "coordination_calls": c[5],
                 "updated_at": now} for sid, c in sorted(computed.items())]
        with db.begin() as conn:
            conn.execute(stmt, rows)
            _set_lines_complete(conn, {sid: c[3] for sid, c in computed.items()})


def _set_lines_complete(conn, wanted: dict) -> None:
    """`lines_complete` of each session of `wanted` ({session: lines DuckDB holds}): true when
    `session_lines` holds as many rows, archived parts included (TR-1506), else false. DuckDB's
    count was read under the lock that `Store.append` holds from its commit to the end of
    `record`, so neither side moves meanwhile."""
    sids = sorted(wanted)
    have: dict = {}
    # one statement, so one snapshot on Postgres: a session the archive moves between two queries
    # (rows deleted, part inserted, one transaction) would be counted twice or not at all
    kept = select(func.count()).where(session_lines.c.session == sessions.c.session).scalar_subquery()
    archived = select(func.coalesce(func.sum(session_archive_parts.c.lines), 0)).where(
        session_archive_parts.c.session == sessions.c.session).scalar_subquery()
    for i in range(0, len(sids), 500):
        have.update(conn.execute(select(sessions.c.session, kept + archived).where(
            sessions.c.session.in_(sids[i:i + 500]))).all())
    for flag in (True, False):
        mine = [sid for sid in sids if (have.get(sid, 0) == wanted[sid]) is flag]
        for i in range(0, len(mine), 500):
            conn.execute(update(sessions).where(sessions.c.session.in_(mine[i:i + 500]))
                         .values(lines_complete=flag))


def lines_size(store) -> tuple[int, int]:
    """(rows, bytes) of `session_lines`, for the gauges: the row count, and the table's size with
    its indexes on Postgres (`pg_total_relation_size`) or the whole file's size on SQLite (pages
    times page size: SQLite has no size per table without a build option)."""
    db = store.sessions_db
    with db.begin() as conn:
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


def _facts_mark(store):
    with store.sessions_db.begin() as conn:
        return conn.execute(select(session_ingest_marks.c.at).where(
            session_ingest_marks.c.name == FACTS_MARK)).scalar_one_or_none()


def _set_facts_mark(store, at: datetime) -> None:
    db = store.sessions_db
    stmt = db.upsert(session_ingest_marks).values(name=FACTS_MARK, at=at)
    stmt = stmt.on_conflict_do_update(index_elements=["name"], set_={"at": stmt.excluded.at})
    with db.begin() as conn:
        conn.execute(stmt)


def fill_facts(store, chunk: int = CHUNK) -> int:
    """Write the facts of the lines DuckDB holds that this table has not seen: every tool_use and
    tool_result line of a claude_code source newer than the `session_facts` mark minus the margin
    (all of them on the first pass). The same functions `record` runs, written as an upsert, so a
    line `record` already wrote, or a pass that runs twice, changes nothing. Reads DuckDB for
    `chunk` sessions at a time, taking and letting go of the store's lock for each; the mark moves
    to the newest `ingest_time` read once every chunk is written, so a pass that raised is read
    again from the same place. Returns how many sessions it looked at."""
    sources = sorted(claude_code_sources(store))
    marks = ", ".join("?" for _ in sources)
    mark = _facts_mark(store)
    since = mark - MARGIN if mark is not None else None
    with store._lock:
        found = store.con.execute(
            f"SELECT key_value, max(ingest_time) FROM events WHERE source IN ({marks}) "
            "AND event_type IN ('tool_use', 'tool_result') "
            "AND key_value IS NOT NULL AND key_value <> '' "
            "AND (CAST(? AS TIMESTAMPTZ) IS NULL OR ingest_time >= ?) GROUP BY key_value",
            [*sources, since, since]).fetchall()
    newest = max((r[1] for r in found), default=None)
    sids = sorted(r[0] for r in found if holds_session(store, r[0]))
    for i in range(0, len(sids), chunk):
        _fill_chunk(store, sources, sids[i:i + chunk], since)
    if newest is not None:
        _set_facts_mark(store, _utc(newest))
    return len(sids)


def _fill_chunk(store, sources: list, sids: list, since) -> None:
    marks = ", ".join("?" for _ in sources)
    keys = ", ".join("?" for _ in sids)
    with store._lock:
        # the tool_use lines that name a tool the facts come from: a cheap filter before the parse
        lines = store.con.execute(
            "SELECT source, key_value, event_time, event_type, text, payload FROM events "
            f"WHERE source IN ({marks}) AND key_value IN ({keys}) "
            "AND (CAST(? AS TIMESTAMPTZ) IS NULL OR ingest_time >= ?) "
            "AND (event_type = 'tool_result' OR (event_type = 'tool_use' "
            "AND (text LIKE '%SendMessage%' OR text LIKE '%Bash(%'))) ORDER BY event_time",
            [*sources, *sids, since, since]).fetchall()
    facts = facts_of(lines)
    if not any(facts.values()):
        return
    db = store.sessions_db
    with db.begin() as conn:
        _write_facts(conn, db, facts)


def _turns_mark(store):
    with store.sessions_db.begin() as conn:
        return conn.execute(select(session_ingest_marks.c.at).where(
            session_ingest_marks.c.name == TURNS_MARK)).scalar_one_or_none()


def _set_turns_mark(store, at: datetime) -> None:
    db = store.sessions_db
    stmt = db.upsert(session_ingest_marks).values(name=TURNS_MARK, at=at)
    stmt = stmt.on_conflict_do_update(index_elements=["name"], set_={"at": stmt.excluded.at})
    with db.begin() as conn:
        conn.execute(stmt)


def fill_turns(store, chunk: int = CHUNK) -> int:
    """Write the turns, last words and touched files of the lines DuckDB holds that these tables
    have not seen: the sessions of a claude_code source with a user, assistant or tool_use line
    newer than the `session_turns` mark minus the margin (all of them on the first pass). The same
    functions `record` runs, written so a line `record` already wrote, or a pass that runs twice,
    changes nothing. Reads DuckDB for `chunk` sessions at a time, taking and letting go of the
    store's lock for each, so one chunk is in memory at once; the mark moves to the newest
    `ingest_time` read once every chunk is written, so a pass that raised is read again from the
    same place. Returns how many sessions it looked at."""
    sources = sorted(claude_code_sources(store))
    marks = ", ".join("?" for _ in sources)
    mark = _turns_mark(store)
    since = mark - MARGIN if mark is not None else None
    with store._lock:
        found = store.con.execute(
            f"SELECT key_value, max(ingest_time) FROM events WHERE source IN ({marks}) "
            "AND event_type IN ('user', 'assistant', 'tool_use') "
            "AND key_value IS NOT NULL AND key_value <> '' "
            "AND (CAST(? AS TIMESTAMPTZ) IS NULL OR ingest_time >= ?) GROUP BY key_value",
            [*sources, since, since]).fetchall()
    newest = max((r[1] for r in found), default=None)
    sids = sorted(r[0] for r in found if holds_session(store, r[0]))
    for i in range(0, len(sids), chunk):
        _fill_turns_chunk(store, sources, sids[i:i + chunk], since)
    if newest is not None:
        _set_turns_mark(store, _utc(newest))
    return len(sids)


def _fill_turns_chunk(store, sources: list, sids: list, since) -> None:
    marks = ", ".join("?" for _ in sources)
    keys = ", ".join("?" for _ in sids)
    base = f"FROM events WHERE source IN ({marks}) AND key_value IN ({keys}) "
    args = [*sources, *sids]
    cols = "source, key_value, event_time, event_type, text, payload, labels "
    with store._lock:
        # every user line newer than the mark: a turn is written once, so a repeat is skipped
        lines = store.con.execute(
            f"SELECT {cols}{base}AND event_type = 'user' "
            "AND (CAST(? AS TIMESTAMPTZ) IS NULL OR ingest_time >= ?) ORDER BY event_time",
            [*args, since, since]).fetchall()
        # the newest assistant lines of each session, whatever the mark: only the last few count
        lines += store.con.execute(
            f"SELECT {cols}{base}AND event_type = 'assistant' "
            "QUALIFY row_number() OVER (PARTITION BY source, key_value "
            f"ORDER BY event_time DESC) <= {SAID_READ}", args).fetchall()
        # the newest lines that edit a file (the cheap text filter of `files_facts`), same
        lines += store.con.execute(
            f"SELECT {cols}{base}AND event_type = 'tool_use' "
            "AND (text LIKE '%Edit(%' OR text LIKE '%Write(%') "
            "QUALIFY row_number() OVER (PARTITION BY source, key_value "
            f"ORDER BY event_time DESC) <= {FILES_READ}", args).fetchall()
    rows = turns_of(lines)
    if not any(rows.values()):
        return
    db = store.sessions_db
    with db.begin() as conn:
        _write_turns(conn, db, rows)


def _reconcile_pass(store, t0: float) -> None:
    # one pass at a time: the start-up pass can still be running when the first cycle comes round
    with store.__dict__.setdefault("_reconcile_pass_lock", threading.Lock()):
        try:
            n = reconcile_sessions(store)
            if n:          # a start with nothing to catch up on prints nothing
                print(f"taresd: sessions table caught up for {n} session(s) in "
                      f"{time.perf_counter() - t0:.1f}s", flush=True)
        except Exception as e:
            print(f"taresd: sessions table catch-up failed ({type(e).__name__}: {e}); "
                  "tried again at the next start", flush=True)
        refresh_lines_gauges(store)


def _cycle(ref, stop: threading.Event) -> None:
    """After the first pass: `reconcile_sessions` again every `CYCLE` seconds, so a session that
    `record` wrote in full becomes `lines_complete` within minutes, and the gauges of
    `session_lines` follow. Ends when `Store.close` sets `stop`, or when the store is gone: the
    thread holds a weak reference, so a process that exits without closing its store (the daemon)
    is not kept from freeing its DuckDB connection by a thread parked in `stop.wait`."""
    while not stop.wait(CYCLE):
        store = ref()
        if store is None:
            return
        _reconcile_pass(store, time.perf_counter())
        del store


def start_reconcile(store) -> threading.Thread:
    """Run `reconcile_sessions`, `fill_facts` and then `fill_turns` in a background thread, once
    (the thread returned), and `reconcile_sessions` again every ten minutes in another. A failure
    prints one line: the next pass tries again, and the tables only undercount until then."""
    def run():
        _reconcile_pass(store, time.perf_counter())
        t1 = time.perf_counter()
        try:
            n = fill_facts(store)
            if n:
                print(f"taresd: session facts filled for {n} session(s) in "
                      f"{time.perf_counter() - t1:.1f}s", flush=True)
        except Exception as e:
            print(f"taresd: session facts fill failed ({type(e).__name__}: {e}); "
                  "tried again at the next start", flush=True)
        t2 = time.perf_counter()
        try:
            n = fill_turns(store)
            if n:
                print(f"taresd: session turns filled for {n} session(s) in "
                      f"{time.perf_counter() - t2:.1f}s", flush=True)
        except Exception as e:
            print(f"taresd: session turns fill failed ({type(e).__name__}: {e}); "
                  "tried again at the next start", flush=True)
    th = threading.Thread(target=run, name="sessions-reconcile", daemon=True)
    th.start()
    stop = getattr(store, "_reconcile_stop", None)
    if stop is None:
        stop = store._reconcile_stop = threading.Event()
        threading.Thread(target=_cycle, args=(weakref.ref(store), stop), name="sessions-reconcile-cycle",
                         daemon=True).start()
    return th
