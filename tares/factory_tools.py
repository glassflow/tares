"""The tools a factory cell works with, said once for Settings and the home (TR-1013).

Each tool is a row: {key, name, connected, detail, short, covers, href}. `detail` says how it is
connected ("Connected as the GitHub App on glassflow"), `short` is the part the home's chip shows
after the name ("glassflow"), and `covers` says what it is used for here, from what the cell
already stores: the repos the open factory projects build in (GitHub), the projects that sync with
Linear, the channels the cell's agents post to (Slack), the last session (Claude Code). A private
project the caller is not on is left out of every `covers`. Every string is worded for people;
account, repo, project and channel names are the person's own text.
"""
from __future__ import annotations

import json

from .factory_home import _count, _dt, plain_ago

REPORTING_DAYS = 7
ADV = "/settings/advanced"
_SHOWN = 3


def _text(v) -> str:
    return " ".join(str(v or "").split())


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _list(items: list[str]) -> str:
    """"a", "a and b", "a, b and 3 more"."""
    if len(items) <= _SHOWN:
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
    return ", ".join(items[:_SHOWN]) + f" and {len(items) - _SHOWN} more"


def _open_projects(store, hidden) -> list[dict]:
    return [p for p in _safe(store.list_projects, []) or []
            if p.get("kind") == "software_factory" and p.get("status") != "done" and p["id"] not in hidden]


def github(store, hidden=()) -> dict:
    creds = _safe(store.list_github_credentials, []) or []
    row = {"key": "github", "name": "GitHub", "connected": bool(creds), "href": f"{ADV}?tab=github",
           "short": None, "covers": None}
    if not creds:
        return {**row, "detail": "Not connected"}
    c = creds[0]
    who = _text(c.get("account") or c.get("name"))
    kind = str(c.get("kind") or "")
    if kind.startswith("app"):
        detail = f"Connected as the GitHub App on {who}" if who else "Connected as a GitHub App"
    else:
        detail = f"Connected as {who}" if who else "Connected with a token"
    if len(creds) > 1:
        detail += f", and {_count(len(creds) - 1, 'more connection')}"
    repos: list[str] = []
    for p in _open_projects(store, hidden):
        ho = _safe(lambda: store.get_handover(p["id"]), None) or {}
        for r in ho.get("repos") or []:
            name = _text(r.get("name"))
            if name and name not in repos:
                repos.append(name)
    covers = (f"The open projects build in {_list(sorted(repos))}" if repos
              else "No open project builds in a repo yet")
    return {**row, "detail": detail, "short": who or None, "covers": covers}


def linear(store, linear_mod, hidden=()) -> dict:
    row = {"key": "linear", "name": "Linear", "href": f"{ADV}?tab=linear", "short": None, "covers": None}
    conn = _safe(lambda: linear_mod.public_connection(store), {}) or {}
    if not conn.get("connected"):
        return {**row, "connected": False, "detail": "Not connected"}
    acct = conn.get("account") if isinstance(conn.get("account"), dict) else {}
    who = _text(acct.get("org") or acct.get("name"))
    synced = [p["name"] for p in _open_projects(store, hidden)
              if _safe(lambda p=p: store.get_project_linear(p["id"]), None)]
    covers = ("No open project keeps its tickets in Linear yet" if not synced
              else f"1 open project keeps its tickets in Linear: {synced[0]}" if len(synced) == 1
              else f"{len(synced)} open projects keep their tickets in Linear: {_list(synced)}")
    return {**row, "connected": True, "short": who or None, "covers": covers,
            "detail": f"Connected to the {who} workspace" if who else "Connected"}


def slack(store) -> dict:
    from . import slack as slack_mod
    row = {"key": "slack", "name": "Slack", "href": f"{ADV}?tab=slack", "short": None, "covers": None}
    token = _safe(lambda: slack_mod.resolve_token(store)[0], "")
    if not token:
        return {**row, "connected": False, "detail": "Not connected"}
    try:
        team = json.loads(store.get_setting("slack_team") or "{}")
    except ValueError:
        team = {}
    who = _text(team.get("name")) if isinstance(team, dict) else ""
    channels = []
    for a in _safe(store.list_catalog_agents, []) or []:
        ch = _text(a.get("slack_channel")).lstrip("#")
        if ch and f"#{ch}" not in channels:
            channels.append(f"#{ch}")
    covers = (f"Agents post to {_list(channels)}" if channels
              else "No agent posts to a channel yet; the standup asks where to post each time")
    return {**row, "connected": True, "short": who or None, "covers": covers,
            "detail": f"Connected to the {who} workspace" if who else "Connected"}


def claude_code(store, now) -> dict:
    row = {"key": "claude_code", "name": "Claude Code", "href": "/sources/claude-code", "short": None,
           "covers": None}
    names = {c.get("name") for c in _safe(store.list_catalog_sources, []) or []
             if c.get("connector") == "claude_code"}
    if not names:
        return {**row, "connected": False,
                "detail": f"Not connected. Nothing has reported in {REPORTING_DAYS} days."}
    last = None
    for s in _safe(store.event_stats, []) or []:
        t = _dt(s.get("last_ingest"))
        if s.get("source") in names and t and (last is None or t > last):
            last = t
    fresh = last is not None and (now - last).total_seconds() <= REPORTING_DAYS * 86400
    if fresh:
        ago = plain_ago(last, now)
        return {**row, "connected": True, "detail": f"Reporting: last session {ago}",
                "short": f"last session {ago}",
                "covers": "Your sessions and the crew's report here, so their work shows on each ticket"}
    return {**row, "connected": False,
            "detail": f"Not connected. Nothing has reported in {REPORTING_DAYS} days."}


def tools(store, now, hidden=()) -> list[dict]:
    """GitHub, Linear, Slack and Claude Code, in that order."""
    from . import linear as linear_mod
    return [github(store, hidden), linear(store, linear_mod, hidden), slack(store),
            claude_code(store, now)]
