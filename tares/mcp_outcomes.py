"""The outcome tools (TR-757, TR-766). The Software Factory edition has what needs me, how a
project is doing, start a project and hand it to the crew. The AI SRE edition has what is broken,
how a service is doing, what happened and mark an incident. Each is one call to the same API the
console reads. Registered on the same server as tares/mcp_server.py, which imports this module at
its end; the edition filter there lists them first on a cell of their edition.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import mcp_factory
from .mcp_server import TARESD, _cx, create_project, mcp, writable


@mcp.tool()
async def what_needs_me() -> str:
    """What is waiting for the person right now: the same list the home page shows under
    "Waiting for you", oldest first with the projects ready to close last, each with what it is, which project it belongs to and how
    long it has waited. Start here when the person asks what to do next or what is stuck."""
    async with _cx(15) as cx:
        r = await cx.get(f"{TARESD}/api/home")
    if r.status_code >= 400:
        return f"error {r.status_code}: {r.text}"
    home = r.json()
    return json.dumps({"headline": home.get("headline"),
                       "waiting_for_you": home.get("waiting_for_you") or []}, default=str)


@mcp.tool()
async def how_is_project(project: str) -> str:
    """How one project is doing, as its row on the home page shows it: where it stands, the
    milestone it is on, tickets merged and blocked out of all, builders working, repos and when
    it last moved. `project` is its name or id."""
    async with _cx(15) as cx:
        r = await cx.get(f"{TARESD}/api/home")
    if r.status_code >= 400:
        return f"error {r.status_code}: {r.text}"
    rows = r.json().get("projects") or []
    hit = next((p for p in rows if project.strip() in (p.get("id"), p.get("name"))), None)
    if hit is None:
        names = ", ".join(p["name"] for p in rows)
        return f"No project called {project}. Projects: {names}." if names else \
            f"No project called {project}. There are no open projects."
    return json.dumps(hit, default=str)


@writable()
async def start_project(name: str, goal: str = "") -> str:
    """Start a new project for a piece of work the person wants built. `name` is its short name,
    unique on Tares; `goal` is one line saying what it is for. It starts empty: spec it next
    (the spec skill), then hand it to the crew with hand_project_to_crew. Returns the project's
    id and name."""
    return await create_project(name, goal)


@writable()
async def hand_project_to_crew(project: str, repos: list[dict] | None = None,
                               autonomy: str = "") -> str:
    """Hand a planned project to the crew to build. `project` is its name or id. `repos` is one
    {path, name} per repo the project changes: `path` the folder it is checked out in on this
    machine, `name` its GitHub repo (owner/name), the repo that holds the plan first. Pass
    `autonomy` only when the person wants this project stricter than the crew, for example
    L3-review, where every merge waits for their yes. Says whether a crew is running and what to
    send it next."""
    return await mcp_factory.hand_over(project=project, repos=repos, autonomy=autonomy)


# ── the AI SRE edition (TR-766) ──────────────────────────────────────────────
WINDOWS = {"30m": timedelta(minutes=30), "1h": timedelta(hours=1), "6h": timedelta(hours=6),
           "24h": timedelta(hours=24)}
INCIDENT_ROUTES = {"on_it": "on_it", "resolved": "resolve", "open": "reopen"}


def _why(r) -> str:
    """A failed answer as a sentence: the daemon's own `detail` for a refusal (403, 404, 409), the
    status and text for anything else."""
    try:
        detail = r.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    text = detail if isinstance(detail, str) and detail else r.text
    return text if r.status_code in (403, 404, 409) else f"error {r.status_code}: {text}"


@mcp.tool()
async def what_is_broken() -> str:
    """What is wrong right now, as the AI SRE home page says it: the headline, whether anybody is
    watching the services and, if not, why and since when, and every open incident with its
    service, how long it has been open, who is on it, what is failing, the likely cause and the
    next step. Start here when the person asks what is broken, what is on fire or whether
    anything needs them."""
    async with _cx(15) as cx:
        r = await cx.get(f"{TARESD}/api/home")
    if r.status_code >= 400:
        return _why(r)
    home = r.json()
    watch = home.get("watch") or {}
    keep = ("id", "title", "service", "state", "age_words", "who_words", "failing",
            "likely_cause", "next_step")
    return json.dumps({
        "headline": home.get("headline"),
        "watch": {k: watch.get(k) for k in ("on", "reason_words", "since")},
        "incidents": [{k: i.get(k) for k in keep} for i in home.get("incidents") or []],
        "more_incidents": home.get("incidents_more") or 0}, default=str)


@mcp.tool()
async def how_is_service(service: str) -> str:
    """How one service is doing, as its page shows it: its state in words, what it sends
    (logs, metrics, traces, deploys), whether it is watched and by whom, when it was last checked,
    its incidents and what the cell cannot see about it. `service` is its name. An unknown name
    answers with the services there are."""
    async with _cx(15) as cx:
        r = await cx.get(f"{TARESD}/api/sre/services/{quote(service.strip(), safe='/')}")
        if r.status_code == 404:
            r = await cx.get(f"{TARESD}/api/sre/services")
            if r.status_code >= 400:
                return _why(r)
            names = ", ".join(s["name"] for s in r.json().get("services") or [])
            return f"No service called {service}. Services: {names}." if names else \
                f"No service called {service}. This Tares has no services yet."
    if r.status_code >= 400:
        return _why(r)
    page = r.json()
    keep = ("name", "state", "state_words", "watch_words", "signals_words", "feeds_words",
            "last_words", "events_24h_words", "incidents_open", "blind_spots_words")
    out = {k: page.get(k) for k in keep}
    out["incidents"] = page.get("incidents") or []
    out["blind_spots"] = [{"title": b.get("title"), "detail": b.get("detail")}
                          for b in page.get("blind_spots") or []]
    return json.dumps(out, default=str)


@mcp.tool()
async def what_happened(service: str = "", around: str = "", window: str = "30m") -> str:
    """What happened to the services in a stretch of time, newest first: log changes, deploys,
    alerts, checks, incidents and metric steps, each with when it was and what it was. `service`
    narrows it to one service. `window` is how far back to look: 30m, 1h, 6h or 24h. The window
    ends at `around`, an ISO time such as 2026-11-02T14:00:00Z (no zone means UTC), which is now
    when left out. To see what led up to a moment, pass that moment as `around`."""
    span = WINDOWS.get((window or "30m").strip())
    if span is None:
        return f"window must be one of {', '.join(WINDOWS)}. You sent {window}."
    try:
        end = datetime.fromisoformat(around.strip().replace("Z", "+00:00")) if around.strip() \
            else datetime.now(timezone.utc)
        end = end.replace(tzinfo=timezone.utc) if end.tzinfo is None \
            else end.astimezone(timezone.utc)
        params = {"from": (end - span).isoformat(), "to": end.isoformat()}
    except (ValueError, OverflowError):
        return f"around must be an ISO time, like 2026-11-02T14:00:00Z. You sent {around}."
    if service.strip():
        params["service"] = service.strip()
    async with _cx(30) as cx:
        r = await cx.get(f"{TARESD}/api/sre/timeline", params=params)
    if r.status_code >= 400:
        return _why(r)
    body = r.json()
    return json.dumps({
        "range": body.get("range_words"), "nothing": body.get("empty_words"),
        "items": [{"time_words": i.get("time_words"), "service": i.get("service"),
                   "text": i.get("text")} for i in body.get("items") or []],
        "more": bool(body.get("next_before"))}, default=str)


@mcp.tool()
async def mark_incident(incident: str, state: str) -> str:
    """Change where an incident stands. `incident` is its id, as what_is_broken shows it. `state`
    is on_it (you or the person is working on it), resolved (it is over) or open (nobody is on it
    and it is not over). It is recorded under the key the caller uses, so a read-only key is
    refused and the answer says so. So is a change the incident's state does not allow: a
    resolved incident cannot go to on_it until it is open again. Returns the incident as it
    stands now."""
    route = INCIDENT_ROUTES.get((state or "").strip())
    if route is None:
        return f"state must be one of {', '.join(INCIDENT_ROUTES)}. You sent {state}."
    async with _cx(15) as cx:
        r = await cx.post(f"{TARESD}/api/sre/incidents/{quote(incident.strip(), safe='')}/{route}")
    if r.status_code >= 400:
        return _why(r)
    inc = r.json()
    return json.dumps({k: inc.get(k) for k in ("id", "title", "service", "state", "state_words", "who")},
                      default=str)
