"""What a click on a Slack button does (TR-762), and how Slack learns of a change made elsewhere.

The buttons are on the messages an AI SRE cell posts (tares/slack.py): "I'm on it", "Resolve" and
"Reopen" on an incident, "Worth raising" and "Not worth it" on what a watcher raised. A click
reaches the daemon as a signed `block_actions` request; the daemon checks the signature, answers
200 at once and calls `dispatch` in the background.

Nothing in a payload is trusted beyond what the signature covers: the incident or run comes from
the button value, a value for another workspace is dropped, and the person's name is escaped
before it goes into any message. A handler returns a plain dict the daemon acts on:

    {"ephemeral": text}      a reply only the clicker sees
    {"incident_id": id}      replace the clicked message with the incident as it reads now
    {"thread": text}         a short reply in the message's thread, saying who did what

`on_change` is the other direction: an incident changed in the console or by the sweep updates its
Slack message and says so in its thread. A change made by a click is skipped there, because the
click already updated the message.
"""
from __future__ import annotations

import json

import httpx

from . import feedback, incidents, loop as loop_mod
from . import slack as slack_mod

GONE_INCIDENT = "This incident is no longer in Tares."
GONE_RESULT = "This result is no longer in Tares."

_tasks: set = set()     # keeps the hook's background tasks referenced


def _value(action: dict) -> dict | None:
    try:
        val = json.loads(action.get("value") or "")
    except (TypeError, ValueError):
        return None
    return val if isinstance(val, dict) else None


def _foreign(val: dict, workspace: str | None) -> bool:
    """A button made for another workspace's cell (a different slug than this cell's)."""
    w = val.get("w")
    return bool(w) and str(w) != (workspace or "")


def _noop(store, action: dict, user: dict, workspace: str | None) -> dict:
    return {}


def _incident(store, action: dict, user: dict, workspace: str | None) -> dict:
    val = _value(action)
    if val is None or _foreign(val, workspace):
        return {}
    iid = str(val.get("i") or "")
    before = store.get_incident(iid) if iid else None
    if before is None:
        return {"ephemeral": GONE_INCIDENT}
    kind = str(action.get("action_id") or "")[len("tares_incident_"):]
    who = (user.get("id"), user.get("name"))
    if kind == "on_it":
        after, said = incidents.take(store, iid, *who, via="slack"), "is on it"
    elif kind == "resolve":
        after, said = incidents.resolve(store, iid, *who, via="slack"), "resolved this"
    elif kind == "reopen":
        after, said = incidents.reopen(store, iid, *who, via="slack"), "reopened this"
    else:
        return {}
    if after is None:
        return {"ephemeral": GONE_INCIDENT}
    out: dict = {"incident_id": iid}
    if (before["state"], before.get("owner")) != (after["state"], after.get("owner")):
        out["thread"] = f"{slack_mod.escape_text(user['name'])} {said}"
    return out


def _worth(store, action: dict, user: dict, workspace: str | None) -> dict:
    val = _value(action)
    if val is None or _foreign(val, workspace):
        return {}
    run_id = str(val.get("r") or "")
    worth = str(action.get("action_id")) == slack_mod.WORTH_ACTIONS["yes"]
    try:
        feedback.answer(store, run_id, worth, user["id"], user["name"], "slack")
    except KeyError:
        return {"ephemeral": GONE_RESULT}
    except feedback.NotRaised as e:
        return {"ephemeral": str(e)}
    who = slack_mod.escape_text(user["name"])
    return {"thread": f"{who} says this was {'worth raising' if worth else 'not worth raising'}"}


# the action id's prefix -> its handler: (store, action, slack user, workspace) -> result dict
HANDLERS: dict = {
    "tares_incident_": _incident,
    "tares_worth_": _worth,
    "tares_link_": _noop,
}


def _user_of(payload: dict) -> dict | None:
    u = payload.get("user")
    if not isinstance(u, dict) or not u.get("id"):
        return None
    name = str(u.get("name") or u.get("username") or u["id"]).strip()[:80]
    return {"id": str(u["id"])[:40], "name": name or str(u["id"])[:40]}


def dispatch(store, payload: dict, workspace: str | None) -> dict:
    """Run the handler of the first action in a verified `block_actions` payload. Does nothing
    ({}) for any other payload and on a cell that is not an AI SRE cell. Blocking: the daemon
    calls it on a worker thread."""
    from .edition import edition
    if not isinstance(payload, dict) or payload.get("type") != "block_actions":
        return {}
    if edition(store)[0] != "sre":
        return {}
    actions = payload.get("actions")
    user = _user_of(payload)
    if not isinstance(actions, list) or not actions or not isinstance(actions[0], dict) \
            or user is None:
        return {}
    action = actions[0]
    action_id = str(action.get("action_id") or "")
    for prefix, handler in HANDLERS.items():
        if action_id.startswith(prefix):
            return handler(store, action, user, workspace) or {}
    return {}


# ── a change made outside Slack ──────────────────────────────────────────────
def _change_words(store, inc: dict) -> str:
    """The thread reply for an incident's latest change (console or sweep)."""
    state = inc["state"]
    if state == "on_it":
        return f"{slack_mod.escape_text(inc.get('owner_name') or 'Someone')} is on it in Tares"
    if state == "resolved":
        if inc.get("resolution") == "itself":
            return "Resolved itself, nothing new for 24 hours"
        by = inc.get("resolved_by_name")
        return f"{slack_mod.escape_text(by)} resolved this in Tares" if by else "Resolved in Tares"
    reopened = [e for e in store.incident_log(inc["id"]) if e["action"] == "reopened"]
    by = reopened[-1]["by_name"] if reopened else None
    return f"{slack_mod.escape_text(by)} reopened this in Tares" if by else "Reopened in Tares"


async def _tell_slack(store, inc: dict) -> None:
    token, _origin = slack_mod.resolve_token(store)
    where = inc.get("slack") or {}
    if not token or not where.get("channel") or not where.get("ts"):
        return
    msg = slack_mod.build_incident_message(store, inc, slack_mod.cell_workspace())
    headers = {"authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=15) as cx:
        if msg is not None:
            r = await cx.post(f"{slack_mod.API_BASE}/chat.update", headers=headers,
                              json={"channel": where["channel"], "ts": where["ts"], **msg})
            _check("chat.update", r)
        r = await cx.post(f"{slack_mod.API_BASE}/chat.postMessage", headers=headers,
                          json={"channel": where["channel"], "thread_ts": where["ts"],
                                "text": _change_words(store, inc), "unfurl_links": False,
                                "unfurl_media": False})
        _check("thread reply", r)


def _check(what: str, r: httpx.Response) -> None:
    try:
        data = r.json()
    except Exception:
        data = None
    ok, error, _retry = slack_mod.classify(r.status_code, data)
    if not ok:
        print(f"[slack] incident {what}: {error}")


async def _guarded(store, inc: dict) -> None:
    try:
        await _tell_slack(store, inc)
    except Exception as e:   # telling Slack must never fail what it follows
        print(f"[slack] incident update: {type(e).__name__}: {e}")


def on_change(store, incident: dict | None, via: str) -> None:
    """incidents.ON_CHANGE hook: an incident changed in the console or by the sweep, so its Slack
    message shows the new state and its thread says who did it. AI SRE cells only. A click on a
    Slack button (via "slack") already updated the message. Never raises."""
    try:
        if via == "slack" or not incident or not (incident.get("slack") or {}).get("ts"):
            return
        from .edition import edition
        if edition(store)[0] != "sre":
            return
        task = loop_mod.spawn(_guarded(store, incident))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
    except Exception as e:
        print(f"[slack] incident hook: {type(e).__name__}: {e}")
