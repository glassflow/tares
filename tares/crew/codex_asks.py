"""What a Codex station asks goes to the desk, and the person's answer goes back to it (TR-1593).

A Codex turn that needs a yes stops and asks: to run a command its sandbox does not allow, to change a file
outside its workspace, or a question in words. A Claude Code station's own classifier decides most of these
from the role's rule sentences; Codex has none, so each one is the person's to decide. Nobody stays connected
to a station (`tares.crew.codex_station` says what was measured), so the watch does this once a round, for
every Codex station, under the station's lock (`step`):

  1. reads what the station asks now (`codex_station.asked`: one visit that joins, reads and leaves);
  2. files a desk item for a request it has not seen, with the station, the command and its folder (or what
     the guard's record holds of a file change), and the request's own names: conversation, turn, call and
     the time it was first asked. What was filed is kept in stations/<station>.codex.asks.json, so a watch
     started again, or a request put again, files nothing twice;
  3. for a request whose item the person answered, gives the station that answer
     (`codex_station.answer`): only to the same request, looked for again on that visit. "Approve" lets the one
     call through, anything else declines it; a question gets the person's words;
  4. withdraws the item of a request the station no longer asks (the turn ended or was interrupted, the
     station was stopped or started again): an answer given later is then never handed to another request.

Whose answer counts: the cell's record of answers (`/api/answers`) holds what a person gave, in the console
or by message, or typed in their own session and found there by the cell. A station cannot write one: the
cell refuses a station's reply to a desk item, and this module never answers an item itself. No grant
answers a request here: a grant is a sentence for a model to read, and nothing matches one against a command.
An item a person withdrew without answering is a no.

What the crew cannot answer is left waiting, with a desk item that says so: a request of a kind it does not
know, and a question marked secret (an answer on the desk would be recorded). The person stops or restarts
the station.

Standard library and the crew's own modules.
"""
from __future__ import annotations

import time

from . import codex as cx
from . import codex_station as cs
from . import locks, state
from .errors import CrewError, StationLockedError
from .paths import CrewPaths

KEPT = "codex.asks.json"   # stations/<station>.codex.asks.json: {key: {ask, desk, label, state, at, ...}}
APPROVE, DECLINE = "Approve", "Decline"
BY_A_PERSON = ("console", "feed", "message", "session")   # where the cell records a person's answer from
CLOSED_KEPT = 50   # answered and gone requests kept per station, newest first
WITHDRAWN = "the person withdrew the question without answering it"


def kept(paths: CrewPaths, station: str) -> dict:
    """What the station asked and what became of it: {key: row}."""
    doc = state.read_json(paths.station_file(station, KEPT), None)
    return {k: v for k, v in doc.items() if isinstance(v, dict)} if isinstance(doc, dict) else {}


def _keep(paths: CrewPaths, station: str, rows: dict) -> None:
    closed = sorted((k for k, r in rows.items() if r.get("state") != "open"), key=lambda k: rows[k].get("at") or 0)
    for k in closed[:-CLOSED_KEPT] if len(closed) > CLOSED_KEPT else []:
        del rows[k]
    state.write_json(paths.station_file(station, KEPT), rows, mode=0o600)


def _cut(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _seen_by_guard(paths: CrewPaths, station: str, call: str) -> dict:
    """The guard's record of one call (it runs before Codex asks), {} when it has none."""
    for line in reversed(cs._record(paths, station)):
        if isinstance(line, dict) and line.get("call") == call:
            return line
    return {}


def item_for(paths: CrewPaths, station: str, ask: dict, project: str = "") -> dict:
    """The desk item for one request: the body of POST /api/desk. Pure but for the guard's record."""
    kind, reason = ask["kind"], _cut(ask.get("reason"), 300)
    options = [APPROVE, DECLINE]
    recommendation = "Approve it if the station's task calls for this; decline it otherwise."
    why = ("The station's turn waits for your answer and nothing runs until then. Approve lets this one call "
           "through. Decline tells the station no, and it goes on without it.")
    if kind == "command":
        question = f"{station} asks to run a command its sandbox does not allow: `{_cut(ask['command'], 400)}`"
        detail = f"Command: {_cut(ask['command'], 900)}\nFolder: {ask['cwd'] or 'not given'}"
    elif kind == "file":
        seen = _seen_by_guard(paths, station, ask["item"])
        what = seen.get("input") if isinstance(seen.get("input"), (str, dict, list)) else None
        question = f"{station} asks to change files outside its workspace"
        if ask.get("root"):
            question += f", and to write under {_cut(ask['root'], 200)} from now on"
        detail = ("The change, as the guard saw it: " + _cut(what if isinstance(what, str) else state.dumps_compact(what), 900)
                  if what else "The crew did not see what the change is: Codex names only the call.")
    elif kind == "question" and not any(q.get("isSecret") for q in ask["questions"] if isinstance(q, dict)):
        asked = [q for q in ask["questions"] if isinstance(q, dict)]
        question = f"{station} asks: " + _cut(" / ".join(str(q.get("question") or "") for q in asked), 800)
        first = asked[0].get("options") if len(asked) == 1 and isinstance(asked[0].get("options"), list) else []
        options = [_cut(o.get("label"), 280) for o in first if isinstance(o, dict) and o.get("label")][:8]
        recommendation = "Answer in your own words, or pick one of its options."
        why = "The station's turn waits for your answer. Your words are handed to it as you give them."
        detail = "\n".join(_cut(f"{q.get('header') or ''}: {q.get('question') or ''}", 400) for q in asked)
    else:
        secret = kind == "question"
        question = (f"{station} asks for a secret, which the crew does not pass on" if secret else
                    f"{station} waits on a request the crew cannot answer ({_cut(ask['method'], 80)})")
        options = []
        recommendation = f"Stop or restart it: tares factory restart {station}"
        why = ("Its turn waits and nothing runs. An answer given here does not reach it; this item is withdrawn "
               "when the station no longer waits.")
        detail = ""
    if reason:
        question += f". Its reason: {reason}"
    names = (f"Codex station {station}, conversation {ask['thread']}, turn {ask['turn']}, call {ask['item']}, "
             f"first asked at {_stamp(ask.get('started'))}. Request {ask['key']}.")
    body = {"question": _cut(question, 990), "recommendation": recommendation, "why": why, "options": options,
            "context": (detail + "\n" + names).strip()[:1490], "station": station, "blocking": True,
            "asked_by": f"{station} (asked by Codex, filed by the crew's watch)"}
    if project:
        body["project"] = project
    return body


def _stamp(ms) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ms) / 1000))
    except (TypeError, ValueError, OverflowError, OSError):
        return "a time Codex did not give"


def answerable(ask: dict) -> bool:
    if ask["kind"] in ("command", "file"):
        return True
    return ask["kind"] == "question" and not any(q.get("isSecret") for q in ask["questions"] if isinstance(q, dict))


def result_for(ask: dict, said) -> dict:
    """What the station is told. `said` is the person's answer as the cell recorded it, or None for an item
    withdrawn without one."""
    if ask["kind"] == "question":
        words = WITHDRAWN if said is None else str(said)
        return {"answers": {str(q.get("id")): {"answers": [words]} for q in ask["questions"] if isinstance(q, dict)}}
    yes = said is not None and " ".join(str(said).lower().split()).strip(" .!\"'") == APPROVE.lower()
    return {"decision": "accept" if yes else "decline"}


# ---- the cell ----

def _json(client, method: str, path: str, body=None):
    return state.loads_json(client.tares_http(method, path, body))


def _filed(client, key: str):
    """The open desk item that already names this request, or None: a watch that ended between filing an item
    and writing it down must not file a second one."""
    for d in client.desk_json():
        if isinstance(d, dict) and f"Request {key}." in str(d.get("context") or ""):
            return d
    return None


def _decided(client, row: dict):
    """(True, the person's words) once a person answered the item, (True, None) when it was withdrawn,
    (False, None) while it waits. CrewError when the cell cannot say."""
    doc = _json(client, "GET", f"/api/answers?item=question:{row['label']}")
    for a in (doc.get("answers") if isinstance(doc, dict) else None) or []:
        if isinstance(a, dict) and a.get("via") in BY_A_PERSON and a.get("by"):
            return True, str(a.get("said") or a.get("choice") or "")
    gone = _json(client, "GET", "/api/desk?state=withdrawn")
    for d in (gone.get("desk") if isinstance(gone, dict) else None) or []:
        if isinstance(d, dict) and d.get("id") == row.get("desk"):
            return True, None
    return False, None


def _withdraw(client, row: dict, reason: str) -> None:
    try:
        client.tares_http("POST", f"/api/desk/{row['label']}/withdraw", {"reason": reason})
    except CrewError:
        pass   # answered or withdrawn meanwhile: there is nothing left to withdraw


# ---- the watch's step ----

def _station(client, paths: CrewPaths, station: str, project: str) -> list:
    did = []
    rows = kept(paths, station)
    was = state.dumps_compact(rows)
    now = cs.asked(paths, station) if cs.is_alive(paths, station) else []
    if now is None:   # it runs and could not be asked: nothing is known, so nothing is changed
        return did
    asks = {a["key"]: a for a in now}
    for key, row in rows.items():
        if row.get("state") == "open" and key not in asks:
            _withdraw(client, row, f"{station} no longer asks this: its turn ended or it was stopped")
            row.update(state="gone", at=time.time())
            state.event("ask-gone", station, f"{row.get('label')} {key}", paths=paths)
            did.append(f"{station} no longer asks {row.get('label')}; the item is withdrawn")
    for key, ask in asks.items():
        row = rows.get(key)
        if row is None:
            item = _filed(client, key) or _json(client, "POST", "/api/desk", item_for(paths, station, ask, project))
            if not isinstance(item, dict) or not item.get("label"):
                raise CrewError(f"Tares gave no desk item for what {station} asks")
            rows[key] = {"ask": ask, "desk": item.get("id"), "label": item["label"], "state": "open", "at": time.time()}
            state.event("asked", station, f"{item['label']} {ask['kind']} {key}", paths=paths)
            did.append(f"{station} waits for an answer: {item['label']} on the desk")
            continue
        if row.get("state") != "open" or not answerable(ask):
            continue
        decided, said = _decided(client, row)
        if not decided:
            continue
        result = result_for(ask, said)
        got = cs.answer(paths, station, key, result)
        if got == "unreachable":   # the answer is the cell's to keep: asked for again next round
            continue
        shown = result.get("decision") or "answered"
        row.update(state="answered" if got == "answered" else "gone", at=time.time(), gave=shown)
        state.event("ask-answered" if got == "answered" else "ask-gone", station, f"{row['label']} {shown} {key}", paths=paths)
        did.append(f"{station} was given the answer to {row['label']} ({shown})" if got == "answered" else
                   f"{station} no longer asks {row['label']}; the answer was not given")
    if state.dumps_compact(rows) != was:
        _keep(paths, station, rows)
    return did


def step(client, paths: CrewPaths, say=None) -> list:
    """One round for every Codex station of the crew (see the module's note). Returns what it did, one line
    each, and says each through `say`. A station another command holds is left for the next round; a cell
    that does not answer leaves everything as it is."""
    did = []
    for station, row in sorted(state.stations_load(paths).items()):
        if not isinstance(row, dict) or row.get("runtime") != cx.RUNTIME:
            continue
        with locks.scope():
            try:
                locks.station_lock(station, paths)
            except StationLockedError:
                continue
            try:
                did += _station(client, paths, station, str(row.get("project_id") or ""))
            except CrewError as e:
                did.append(f"what {station} asks could not be handled this round: {e}")
    for line in did if say else []:
        say(line)
    return did
