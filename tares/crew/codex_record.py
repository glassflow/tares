"""Recording a Codex station's session (TR-1595): what the station did, sent to Tares by visits.

A Claude Code station is recorded by the plugin's hooks, which post its transcript lines. A Codex station has
none of that, and nothing stays connected to it. So once a watch round `step` looks at every Codex station of
the crew and posts what is new to /ingest/codex, where `tares.connectors.codex` turns it into the session
lines the cell already keeps. What is read, and from where:

  a command's start    the guard's record (`tares.crew.codex_guard`): the hook writes a line before every
                       call it lets through, with the command and its folder. Codex lists a command only
                       once it has ended (measured on 0.171.0: a running command is in no `thread/read`, and
                       one that ran when the app-server died is in no stored conversation), so this line is
                       the only place that says it started.
  a command's end      its item in the conversation (`thread/read` with the turns): the real exit code, the
                       folder, what it printed. An item is listed a moment after it ends, also in the
                       middle of a turn (0.5 s to a few seconds, measured).
  messages, MCP calls  the same items.
  the state            the conversation's status: working, waiting (on an approval or an answer), idle; a
                       station that is not running has ended.
  tokens               the `token_count` lines Codex writes in the conversation's own file, one per reply.
                       The app-server gives a visitor no count (`account/usage/read` answers with none for
                       a conversation, measured), and that file's format is Codex's own, so a line that is
                       not of the shape of 0.171.0 is passed over: the tokens are then unknown, never 0.

A start with no item is not left out. While its turn runs it is a command that started. Once the turn has
ended, or the conversation was given up, and no item came, a fact says its outcome is unknown: the app-server
died while it ran, or just after, before it wrote the end down.

What was sent is kept in stations/<station>.codex.sent, written after Tares took the post. A post that fails
is sent again next round; one whose answer was lost is sent twice, and the cell's facts take it once (a
command's row is keyed by conversation and call).

`step` never raises and never changes a station. It takes no lock: it only reads, and a station that is
stopped or started under it reads as not running for this round.

The watch runs `step` first in its Codex step, and that step first in the round (TR-1593): before revive,
regrant, idle wake or pace can start another turn, what the last one did is recorded. A stop records last
(`tares.crew.codex_station.stop`, which every stop goes through: restart, retire, crew down, a station found
running past its guard): one look while the app-server still answers, then, once it has ended, `close_station`
says every start that has no end has an outcome nobody will know. A closing Tares did not take is kept and
sent again by the next look, whether or not the station runs again.
"""
from __future__ import annotations

import json
import os
import socket
import time

from . import codex_guard, state
from . import codex_station as cs
from .codex_wire import WireError
from .errors import CrewError
from .paths import CrewPaths

SENT = "codex.sent"        # stations/<station>.codex.sent: what was sent of the station's conversation
OUTPUT_KEPT = 20000        # characters of a command's output sent: its end, where a test run sums up
TEXT_KEPT = 20000          # characters of a message sent
POST_FACTS = 50            # facts in one post
SETTLE = 5.0               # seconds a start may be newer than the newest listed turn and still wait for its own
STATES = {"active": "working", "waiting": "waiting", "idle": "idle", "notLoaded": "idle"}


def machine(env=None) -> str:
    """The machine the crew runs on, as the plugin's recorder names it: FACTORY_MACHINE, else the host name
    up to its first dot."""
    env = os.environ if env is None else env
    return (env.get("FACTORY_MACHINE") or "").strip() or socket.gethostname().split(".")[0].strip()


def stamps(paths: CrewPaths, station: str, row: dict, env=None) -> dict:
    """What every fact of a station carries: who it is, its crew, machine, project and folder."""
    doc = state.read_json(paths.station_file(station, "settings.json"), {})
    senv = doc.get("env") if isinstance(doc, dict) and isinstance(doc.get("env"), dict) else {}
    out = {"factory_station": station, "factory_crew": str(senv.get("FACTORY_CREW") or state.crew_name(paths)),
           "factory_machine": machine(env), "cwd": str(row.get("cwd") or ""), "model": str(row.get("model") or ""),
           "station": station, "role": str(row.get("role") or ""), "parent": str(row.get("parent") or "")}
    project = str(senv.get("TARES_PROJECT") or "")
    if project:
        out["tares_project"] = project
    return {k: v for k, v in out.items() if v}


def _stamp(ts=None) -> str:
    ts = time.time() if ts is None else ts
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1000):03d}Z"


def new_sent(thread: str = "") -> dict:
    """What is kept of one conversation: `starts` the calls whose start was sent and whose end was not,
    by call ({turn, at}); `items` the items sent of turns that are not closed (and the calls settled
    without a turn); `seen` the turns it listed; `ended` the turns seen ended at the last look; `closed`
    the turns all of whose items were sent; `usage` the replies whose tokens were sent; `path` its own
    file; `before` the conversation the station had before this one; `closing` why a stop closed its open
    starts, while Tares has not taken that closing yet, with `untold` the starts it found that no look sent."""
    return {"thread": thread, "before": "", "announced": False, "state": "", "starts": {}, "items": [],
            "closed": [], "ended": [], "seen": [], "usage": 0, "path": "", "closing": "", "untold": []}


def _fact(base: dict, kind: str, at=None, **more) -> dict:
    return {"type": "codex", "v": 1, "kind": kind, "timestamp": _stamp(at), **base, **more}


def _cut(text, limit: int, tail: bool = False):
    text = text if isinstance(text, str) else ""
    return text if len(text) <= limit else (text[-limit:] if tail else text[:limit])


def _slim(item: dict) -> dict:
    """An item as it is sent: a long output keeps its end, a long message its start."""
    out = dict(item)
    if isinstance(out.get("aggregatedOutput"), str):
        out["aggregatedOutput"] = _cut(out["aggregatedOutput"], OUTPUT_KEPT, tail=True)
    if isinstance(out.get("text"), str):
        out["text"] = _cut(out["text"], TEXT_KEPT)
    out.pop("memoryCitation", None)
    return out


def _mcp_name(item: dict) -> str:
    return f"mcp__{item.get('server') or ''}__{item.get('tool') or ''}"


def _turn_began(turn: dict) -> float:
    try:
        return float(turn.get("startedAt") or 0)
    except (TypeError, ValueError):
        return 0.0


def facts(found, record: list, sent: dict, base: dict, alive: bool, now: float | None = None) -> tuple:
    """(the facts to send, what will have been sent once they are taken). Pure.

    `found` is the conversation as `thread/read` gave it with its turns, or None when it could not be read
    (the station is not running, or did not answer). `record` is the guard's record, `sent` what was sent
    before (`new_sent`), `base` what every fact carries (`sessionId` and the station's stamps), `alive`
    whether the station's app-server runs."""
    now = time.time() if now is None else now
    sent = json.loads(json.dumps(sent))   # the caller keeps the old one until the post is taken
    out = []
    turns = [t for t in (found or {}).get("turns") or [] if isinstance(t, dict) and t.get("id")]
    if not sent["announced"]:
        # dated at the conversation's own start when that is known, so it comes before what was said in it
        try:
            born = float((found or {}).get("createdAt") or 0)
        except (TypeError, ValueError):
            born = 0.0
        out.append(_fact(base, "session", born or now))
        sent["announced"] = True

    listed = {t["id"]: t for t in turns}
    for tid in listed:
        if tid not in sent["seen"]:
            sent["seen"].append(tid)
    status = (found or {}).get("status") if isinstance((found or {}).get("status"), dict) else {}
    kind = "waiting" if status.get("type") == "active" and status.get("activeFlags") else status.get("type")
    newest = max((_turn_began(t) for t in turns), default=0.0)
    created = float((found or {}).get("createdAt") or 0)

    # the items each listed turn has that were not sent: their ends, and a start for one the guard's record
    # did not give (a command Codex ran for itself, a record that was trimmed)
    done_items = set(sent["items"])
    item_ids = set()
    ends = []
    for turn in turns:
        tid = turn["id"]
        if tid in sent["closed"]:
            continue
        for item in turn.get("items") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            iid, typ = str(item["id"]), item.get("type")
            item_ids.add(iid)
            if iid in done_items:
                continue
            if typ in ("commandExecution", "mcpToolCall") and item.get("status") == "inProgress":
                continue   # not ended yet: it is sent when it has
            ends.append((tid, turn, item))

    # the starts the guard's record holds that were not sent
    for line in record:
        if not (isinstance(line, dict) and line.get("call") and line.get("said") == "allow" and line.get("tool") == "Bash"):
            continue
        call, tid = str(line["call"]), str(line.get("turn") or "")
        if call in sent["starts"] or call in done_items or tid in sent["closed"]:
            continue
        at = float(line.get("at") or now)
        start = _fact(base, "start", at, call=call, turn=tid, tool="Bash", command=line.get("command") or "",
                      command_cwd=line.get("cwd") or "", **({"cut": True} if line.get("cut") else {}))
        if tid in sent["seen"]:
            out.append(start)
            sent["starts"][call] = {"turn": tid, "at": at}
        elif found is not None and created and at < created:
            # older than this conversation: it started in the one before, after the last look at it, and
            # that one was given up, so nobody will list how it ended
            if sent.get("before"):
                earlier = {**base, "sessionId": sent["before"]}
                out.append({**start, "sessionId": sent["before"]})
                out.append(_fact(earlier, "unknown", now, call=call, turn=tid,
                                 why="the conversation was given up before its end was recorded"))
            sent["items"].append(call)
        elif found is not None and (kind in ("idle", "notLoaded") or at < newest - SETTLE):
            # its turn is in no stored conversation and will not be: the app-server died before it wrote
            # the turn down
            out.append(start)
            out.append(_fact(base, "unknown", now, call=call, turn=tid,
                             why="its turn is not in the stored conversation"))
            sent["items"].append(call)
        # else: its turn is not listed yet (a turn that just began, or a station that cannot be read now)

    for tid, turn, item in ends:
        iid, typ = str(item["id"]), item.get("type")
        if typ == "commandExecution" and iid not in sent["starts"]:
            began = (item.get("startedAtMs") or 0) / 1000 or _turn_began(turn) or now
            out.append(_fact(base, "start", began, call=iid, turn=tid, tool="Bash",
                             command=codex_guard.asked(item.get("command")), command_cwd=item.get("cwd") or ""))
        if typ == "mcpToolCall":
            began = (item.get("startedAtMs") or 0) / 1000 or _turn_began(turn) or now
            args = item.get("arguments")
            out.append(_fact(base, "start", began, call=iid, turn=tid, tool=_mcp_name(item),
                             arguments=args if isinstance(args, dict) else {}))
        if typ in ("userMessage", "agentMessage", "plan", "commandExecution", "mcpToolCall", "fileChange"):
            out.append(_fact(base, "item", now, turn=tid, turn_status=turn.get("status"), item=_slim(item)))
        sent["starts"].pop(iid, None)
        sent["items"].append(iid)

    # a turn that has ended: at the look after the one that first saw it ended, a start with no item has an
    # outcome nobody will know, and the turn is closed
    for turn in turns:
        tid = turn["id"]
        if tid in sent["closed"] or turn.get("status") == "inProgress":
            continue
        if tid not in sent["ended"]:
            sent["ended"].append(tid)
            continue
        for call, start in list(sent["starts"].items()):
            if start.get("turn") == tid and call not in item_ids:
                out.append(_fact(base, "unknown", now, call=call, turn=tid,
                                 why=f"its turn ended ({turn.get('status')}) with no record of how the command ended"))
                del sent["starts"][call]
        ids = {str(i.get("id")) for i in turn.get("items") or [] if isinstance(i, dict)}
        sent["items"] = [i for i in sent["items"] if i not in ids]
        sent["closed"].append(tid)
        sent["ended"].remove(tid)

    said = "ended" if not alive else STATES.get(kind or "", "")
    if said and said != sent["state"]:
        why = ("the station is not running" if said == "ended" else
               ", ".join(str(f) for f in status.get("activeFlags") or []) if said == "waiting" else "")
        out.append(_fact(base, "state", now, state=said, reason=why))
        sent["state"] = said
    return out, sent


def closing(sent: dict, base: dict, why: str, now: float | None = None) -> list:
    """The last facts of a conversation that will not be read again (it was given up for a new one, or its
    station is gone): every start with no end has an unknown outcome, and the session has ended."""
    now = time.time() if now is None else now
    out = [_fact(base, "unknown", now, call=call, turn=start.get("turn") or "", why=why)
           for call, start in sorted(sent.get("starts", {}).items())]
    if sent.get("state") != "ended":
        out.append(_fact(base, "state", now, state="ended", reason=why))
    return out


def tokens(path: str, already: int, base: dict) -> tuple:
    """(usage facts for the replies after the first `already`, how many replies the file holds now), from
    the conversation's own file. Only a line shaped as 0.171.0 writes it counts: `payload.type` is
    `token_count` and `payload.info.last_token_usage` holds whole numbers. Anything else is passed over, and
    a file that cannot be read gives ([], already)."""
    out, n = [], 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                if '"token_count"' not in raw:
                    continue
                try:
                    doc = json.loads(raw)
                except ValueError:
                    continue
                body = doc.get("payload") if isinstance(doc, dict) and isinstance(doc.get("payload"), dict) else {}
                info = body.get("info") if body.get("type") == "token_count" else None
                last = info.get("last_token_usage") if isinstance(info, dict) else None
                if not isinstance(last, dict) or not any(isinstance(v, int) and not isinstance(v, bool)
                                                         for v in last.values()):
                    continue
                n += 1
                if n <= already:
                    continue
                usage = {k: last[k] for k in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                                              "output_tokens", "reasoning_output_tokens")
                         if isinstance(last.get(k), int) and not isinstance(last.get(k), bool)}
                fact = {"type": "codex", "v": 1, "kind": "usage", **base, "n": n, "usage": usage,
                        "timestamp": doc.get("timestamp") if isinstance(doc.get("timestamp"), str) else _stamp()}
                out.append(fact)
    except (OSError, TypeError):
        return [], already
    return out, max(n, already)


# ---------- the round ----------


def _sent_file(paths: CrewPaths, station: str):
    return paths.station_file(station, SENT)


def _load(paths: CrewPaths, station: str) -> dict:
    doc = state.read_json(_sent_file(paths, station), None)
    if not isinstance(doc, dict) or not isinstance(doc.get("starts"), dict):
        return new_sent()
    return {**new_sent(), **doc}


def _keep(paths: CrewPaths, station: str, sent: dict) -> None:
    state.atomic_write_text(_sent_file(paths, station), state.dumps_compact(sent) + "\n", mode=0o600)


def _post(client, found: list) -> None:
    for i in range(0, len(found), POST_FACTS):
        client.tares_http("POST", "/ingest/codex", found[i:i + POST_FACTS])


def close_station(client, paths: CrewPaths, station: str, why: str) -> dict:
    """The station's app-server has ended (a stop): every start with no end has an outcome nobody will know,
    and the session has ended. The conversation stays the station's (a restart carries it on): only its open
    starts are closed. Returns {station, sent, error}; never raises. A closing that is not taken is kept
    (`closing` in what was sent) and sent again by the next look."""
    result = {"station": station, "sent": 0, "error": ""}
    try:
        sent = _load(paths, station)
        row = state.stations_load(paths).get(station) or {}
        sent["thread"] = sent["thread"] or str(row.get("sessionId") or "")
        untold = _untold(paths, station, sent, row)
        if not sent["thread"] or (not sent["starts"] and not untold and sent["state"] == "ended" and not sent.get("closing")):
            return result
        sent["closing"] = why
        sent["untold"] = untold
        _keep(paths, station, sent)
        result["sent"] = _close(client, paths, station, sent)
    except CrewError as e:
        result["error"] = str(e)[:300]
    except (OSError, ValueError) as e:
        result["error"] = f"{type(e).__name__}: {e}"[:300]
    return result


def _untold(paths: CrewPaths, station: str, sent: dict, row: dict) -> list:
    """The starts in the guard's record that no look sent: calls of the station's last turn (or of a turn a
    look saw) let through after the last look, which nobody can read the end of once its app-server is gone."""
    turns = set(sent.get("seen") or []) | {str(row.get("guardLastTurn") or "")}
    out = []
    for line in codex_guard.read_record(codex_guard.record_file(str(paths.raw), station)):
        if not (isinstance(line, dict) and line.get("call") and line.get("said") == "allow" and line.get("tool") == "Bash"):
            continue
        call, tid = str(line["call"]), str(line.get("turn") or "")
        if tid in turns and tid not in sent["closed"] and call not in sent["starts"] and call not in sent["items"]:
            out.append({"call": call, "turn": tid, "at": line.get("at"), "command": line.get("command") or "",
                        "cwd": line.get("cwd") or "", "cut": bool(line.get("cut"))})
    return out


def _close(client, paths: CrewPaths, station: str, sent: dict) -> int:
    """Send the closing kept in `sent`; raises CrewError when Tares does not take it (it stays kept). Each
    call closed is kept among the settled ones, so no later look sends its start again."""
    row = state.stations_load(paths).get(station) or {}
    base = {**stamps(paths, station, row, getattr(client, "env", None)), "sessionId": sent["thread"]}
    out = [] if sent["announced"] else [_fact(base, "session")]
    starts = dict(sent["starts"])
    for u in sent.get("untold") or []:
        out.append(_fact(base, "start", float(u.get("at") or time.time()), call=u["call"], turn=u["turn"], tool="Bash",
                         command=u["command"], command_cwd=u["cwd"], **({"cut": True} if u.get("cut") else {})))
        starts[u["call"]] = {"turn": u["turn"], "at": u.get("at")}
    out += closing({**sent, "starts": starts}, base, sent["closing"])
    if out:
        _post(client, out)
    _keep(paths, station, {**sent, "announced": True, "starts": {}, "untold": [], "state": "ended", "closing": "",
                           "items": list(sent["items"]) + [c for c in starts if c not in sent["items"]]})
    return len(out)


def read(paths: CrewPaths, station: str, thread: str):
    """The conversation with its turns by one visit, or None when the station cannot be read now."""
    try:
        with cs.visit(paths, station) as wire:
            found = wire.request("thread/read", {"threadId": thread, "includeTurns": True}).get("thread")
    except (CrewError, WireError, OSError):
        return None
    return found if isinstance(found, dict) else None


def record_station(client, paths: CrewPaths, station: str, row: dict | None = None) -> dict:
    """One look at one station: post what is new of its conversation. Returns {station, sent, error}; never
    raises. `row` is the station's record (read when not given)."""
    result = {"station": station, "sent": 0, "error": ""}
    try:
        row = row if row is not None else (state.stations_load(paths).get(station) or {})
        thread = str(row.get("sessionId") or "")
        sent = _load(paths, station)
        about = stamps(paths, station, row, getattr(client, "env", None))
        out = []
        if sent["thread"] and sent["thread"] != thread:
            # the conversation was given up for a new one: what it left open is closed, then the new one begins
            old = {**about, "sessionId": sent["thread"]}
            usage, _ = tokens(sent["path"], sent["usage"], old) if sent["path"] else ([], 0)
            out += usage + closing(sent, old, "the conversation was given up before its end was recorded")
            _post(client, out)
            result["sent"] += len(out)
            sent, out = {**new_sent(), "before": sent["thread"]}, []
            _keep(paths, station, sent)
        if sent.get("closing") and sent["thread"] == thread:   # a stop's closing Tares did not take yet
            result["sent"] += _close(client, paths, station, sent)
            sent = _load(paths, station)
        if not thread:
            return result
        sent["thread"] = thread
        base = {**about, "sessionId": thread}
        alive = cs.is_alive(paths, station)
        found = read(paths, station, thread) if alive else None
        if found and isinstance(found.get("path"), str):
            sent["path"] = found["path"]
        if found and found.get("model") and not base.get("model"):
            base["model"] = found["model"]
        record = codex_guard.read_record(codex_guard.record_file(str(paths.raw), station))
        out, after = facts(found, record, sent, base, alive)
        usage, after["usage"] = tokens(after["path"], after["usage"], base) if after["path"] else ([], after["usage"])
        out += usage
        if out:
            _post(client, out)
        if out or after != _load(paths, station):
            _keep(paths, station, after)
        result["sent"] += len(out)
    except CrewError as e:   # Tares did not take the post, or a file of the crew cannot be read: next round
        result["error"] = str(e)[:300]
    except (OSError, ValueError) as e:
        result["error"] = f"{type(e).__name__}: {e}"[:300]
    return result


def _gone(client, paths: CrewPaths, known: dict) -> list:
    """The stations that left a `.codex.sent` and have no record any more (retired): their conversation is
    closed and the file removed."""
    out = []
    try:
        names = [p.name[:-len(SENT) - 1] for p in paths.stations_dir.glob(f"*.{SENT}")]
    except OSError:
        return out
    for station in sorted(names):
        if station in known:
            continue
        result = {"station": station, "sent": 0, "error": ""}
        try:
            sent = _load(paths, station)
            if sent["thread"]:
                base = {"factory_station": station, "factory_crew": state.crew_name(paths),
                        "factory_machine": machine(getattr(client, "env", None)), "sessionId": sent["thread"]}
                usage, _ = tokens(sent["path"], sent["usage"], base) if sent["path"] else ([], 0)
                found = usage + closing(sent, base, "the station was retired")
                _post(client, found)
                result["sent"] = len(found)
            _sent_file(paths, station).unlink(missing_ok=True)
        except CrewError as e:
            result["error"] = str(e)[:300]
        except (OSError, ValueError) as e:
            result["error"] = f"{type(e).__name__}: {e}"[:300]
        out.append(result)
    return out


def step(client, paths: CrewPaths | None = None) -> list:
    """The watch's recording step, once a round: every Codex station of the crew is looked at and what is
    new of its session is posted to Tares. Returns one {station, sent, error} per station looked at. Never
    raises; a station that cannot be read or a post Tares does not take waits for the next round."""
    paths = paths or client.paths
    try:
        rows = state.stations_load(paths)
    except CrewError:
        return []
    known = {name: row for name, row in rows.items() if isinstance(row, dict) and row.get("runtime") == "codex"}
    out = [record_station(client, paths, name, row) for name, row in sorted(known.items())]
    return out + _gone(client, paths, rows)
