"""What Codex's approval reviewer decided for a Codex station, recorded (TR-1599).

A Codex station's requests beyond its sandbox are judged by Codex's own reviewer against the policy the
station is started with: its rule sentences, the person's grants among them
(tares/integrations/builtin/runtime_codex.py, "Grants and approvals"). Nobody stays connected to a station, so
the reviewer's notifications reach no one. Codex keeps each review as a conversation of its own in the
station's Codex folder (`sessions/**/rollout-*.jsonl`), measured on 0.171.0:

  * its turn is started by `guardian_review` (the `turn_trigger` of its `task_started` line), and its first
    line names the station's conversation as its parent;
  * the action it judged is in its prompt, after the text "Planned action JSON:" (for a command:
    {command: [shell, "-lc", script], cwd, justification, ...}; the guard runs the script with its standard
    input closed, `exec </dev/null` first, which is left out here);
  * its decision is its last answer, a JSON object {outcome: allow|deny, risk_level, user_authorization,
    rationale}.

Once a round (the watch's Codex round, `tares.crew.codex_round`), for every Codex station, each review not
recorded yet becomes one event, `codex-review`, and one line from the watch: allowed or refused, the command,
the risk and the reviewer's reason. A review without a decision yet (still running) is left for a later round;
one whose turn ended without a decision (it timed out, or was cut off) is recorded as that: nothing was allowed.
The names of the files looked at are kept in stations/<station>.codex.reviews.json, each for as long as its
file is there, so a watch started again records nothing twice. They are written after the round's events: a
watch killed in between records those again rather than never.

Standard library and the crew's own modules.
"""
from __future__ import annotations

import json
import os

from . import codex as cx
from . import state
from .paths import CrewPaths

KEPT = "codex.reviews.json"   # stations/<station>.codex.reviews.json: [file name, ...]
TRIGGER = "guardian_review"
ENDED = ("task_complete", "turn_aborted")   # a conversation's turn ended, with or without an answer
PLANNED = "Planned action JSON:"
STDIN_CLOSED = "exec </dev/null\n"   # what the guard puts before every command it lets through


def _lines(path: str) -> list:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    doc = json.loads(line)
                except ValueError:
                    continue
                if isinstance(doc, dict):
                    out.append(doc)
    except OSError:
        return []
    return out


def _payload(line: dict) -> dict:
    p = line.get("payload")
    return p if isinstance(p, dict) else {}


def _texts(payload: dict) -> list:
    return [str(c.get("text") or "") for c in payload.get("content") or [] if isinstance(c, dict)]


def _command(action) -> str:
    """The command a planned action runs, as the station wrote it."""
    if not isinstance(action, dict):
        return ""
    cmd = action.get("command")
    if isinstance(cmd, list) and cmd:
        cmd = cmd[-1] if len(cmd) == 3 and cmd[1] in ("-lc", "-c") else " ".join(str(x) for x in cmd)
    text = str(cmd or "")
    if text.startswith(STDIN_CLOSED):
        text = text[len(STDIN_CLOSED):]
    return text or str(action.get("tool") or action.get("type") or "")


def _parent(path: str) -> str:
    """The conversation a conversation file was started from (its first line), "" for one of its own: a
    station's main conversation, read no further."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.loads(f.readline())
    except (OSError, ValueError):
        return ""
    if not isinstance(doc, dict) or doc.get("type") != "session_meta":
        return ""
    return str(_payload(doc).get("parent_thread_id") or "")


NOT_A_REVIEW = {"outcome": ""}   # a conversation started from the station's for something else (a helper agent)


def read(path: str):
    """One review conversation: {file, at, parent, outcome, risk, authorization, why, command}; NOT_A_REVIEW
    for a conversation started for something else; None for one that holds no decision yet."""
    lines = _lines(path)
    triggers = [(_payload(x).get("turn_attribution") or {}).get("turn_trigger") for x in lines
                if _payload(x).get("type") == "task_started"]
    if TRIGGER not in triggers:
        return NOT_A_REVIEW if triggers else None
    meta = next((_payload(x) for x in lines if x.get("type") == "session_meta"), {})
    action = None
    answer = ""
    for x in lines:
        p = _payload(x)
        if p.get("type") != "message":
            continue
        texts = _texts(p)
        if p.get("role") == "user":
            for i, t in enumerate(texts):
                if t.strip() == PLANNED and i + 1 < len(texts):
                    try:
                        action = json.loads(texts[i + 1])
                    except ValueError:
                        action = None
        elif p.get("role") == "assistant" and texts:
            answer = texts[-1]
    try:
        decision = json.loads(answer) if answer else None
    except ValueError:
        decision = None
    if not isinstance(decision, dict) or decision.get("outcome") not in ("allow", "deny"):
        if not any(_payload(x).get("type") in ENDED for x in lines):
            return None   # still running
        decision = {"outcome": "none"}   # it ended without one (it timed out, or was cut off): nothing was allowed
    return {"file": os.path.basename(path), "at": str(meta.get("timestamp") or ""),
            "parent": str(meta.get("parent_thread_id") or ""), "outcome": decision["outcome"],
            "risk": str(decision.get("risk_level") or ""), "authorization": str(decision.get("user_authorization") or ""),
            "why": " ".join(str(decision.get("rationale") or "").split()), "command": _command(action)}


def _files(home: str) -> list:
    out = []
    for root, _dirs, names in os.walk(os.path.join(home, "sessions")):
        out += [os.path.join(root, n) for n in names if n.startswith("rollout-") and n.endswith(".jsonl")]
    return sorted(out, key=os.path.basename)


def line_for(station: str, review: dict) -> str:
    if review["outcome"] == "none":
        return (f"Codex's reviewer ended without a decision for {station}, so it was not allowed: "
                f"{review['command'] or 'an action'}")
    what = "allowed" if review["outcome"] == "allow" else "refused"
    text = f"Codex's reviewer {what} for {station}, by its rules: {review['command'] or 'an action'}"
    if review["risk"]:
        text += f" (risk {review['risk']})"
    return text + (f": {review['why']}" if review["why"] else "")


def new(paths: CrewPaths, station: str) -> list:
    """The reviews of the station not recorded yet, oldest first, each recorded now (an event), and kept as
    recorded."""
    seen = state.read_json(paths.station_file(station, KEPT), None)
    seen = [s for s in seen if isinstance(s, str)] if isinstance(seen, list) else []
    known = set(seen)
    out = []
    changed = False
    for path in _files(str(cx.station_home(paths, station))):
        name = os.path.basename(path)
        if name in known or not _parent(path):
            continue
        review = read(path)
        if review is None:
            continue   # a review still running: looked at again next round
        seen.append(name)
        known.add(name)
        changed = True
        if review is NOT_A_REVIEW:
            continue
        state.event("codex-review", station, line_for(station, review)[:1000], paths=paths)
        out.append(review)
    if changed:
        # every name whose file is still there stays: one dropped would be read, and recorded, again
        there = {os.path.basename(p) for p in _files(str(cx.station_home(paths, station)))}
        state.write_json(paths.station_file(station, KEPT), [s for s in seen if s in there], mode=0o600)
    return out


def step(client, paths: CrewPaths, say=None) -> list:
    """The watch's part: every Codex station's new reviews, recorded and said. Returns [(station, review)]."""
    done = []
    for station, row in sorted(state.stations_load(paths).items()):
        if not isinstance(row, dict) or row.get("runtime") != cx.RUNTIME:
            continue
        for review in new(paths, station):
            done.append((station, review))
            if say:
                say(line_for(station, review))
    return done
