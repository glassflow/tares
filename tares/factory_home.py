"""GET /api/home on a factory cell (TR-729): the frame of the factory home in one call.

A headline, the things that wait for the person, the open projects, the crew, the Claude plan and
the connected tools, all worded for people: every string is shown as it is, so durations are in
plain words ("6 days", never "9007 min") and tests/test_factory_home.py checks the body against
tares/words.py.

The helpers below are shared by the screens that show part of this (`ctx.fx` is the crew readers
from factory_api.register). `WAITING_BUILDERS` is the list of functions that make the waiting
items; TR-769 fills it, so does every other ticket that adds a kind of item.
"""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone

from . import claude_usage
from . import factory as factory_mod
from . import linear as linear_mod
from .envelope import now_utc

CACHE_SECONDS = 5

# the kinds of waiting item; `permission` is a desk item asking to run a command, `signoff`,
# `grant` (a grant waiting for your consent) and `desk` (a desk item addressed to you) come with teams
KINDS = ("prs_to_review", "ready_to_start", "calls_to_confirm", "question", "permission",
         "milestone_to_check", "signoff", "grant", "desk")
# what a button does on the page when it is not a link
ACTIONS = ("copy", "keep_assumptions", "allow", "answer", "sign_off", "agree", "decline",
           "hand_over_anyway", "close")

STATE_LABEL = {"building": "Building", "waiting_on_you": "Waiting on you",
               "not_started": "Not started", "ready_to_close": "Ready to close"}
_STATE_ORDER = {"waiting_on_you": 0, "building": 1, "not_started": 2, "ready_to_close": 3}

ROLE_LINE = {"orchestrator": "Hands out work and brings you what needs you.",
             "reviewer": "Reviews every pull request before you see it.",
             "releaser": "Tags, deploys and checks each release.",
             "builder": "Builds the tickets of its project."}
ROLE_NAME = {"orchestrator": "Orchestrator", "reviewer": "Reviewer", "releaser": "Releaser"}
_ALWAYS_ON = ("orchestrator", "reviewer", "releaser")
_WORKING_STAGES = ("doing", "review", "changes")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_STALE_MINUTES = 60   # an orchestrator silent this long is "not heard from"

# the functions that make waiting items: (store, fx, now) -> [home_item(...), ...]
WAITING_BUILDERS: list = []


# ── words ─────────────────────────────────────────────────────────────────────

def _dt(v) -> datetime | None:
    """A datetime, aware (UTC), from a datetime or an ISO string; None for anything else."""
    if isinstance(v, str) and v:
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(v, datetime):
        return None
    return v if v.tzinfo else v.replace(tzinfo=timezone.utc)


def _iso(v) -> str | None:
    t = _dt(v)
    return t.isoformat() if t else None


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def plain_wait(minutes: int | None) -> str:
    """A span in plain words: "just now", "5 minutes", "1 hour", "3 hours", "1 day", "6 days",
    "3 weeks"."""
    if minutes is None or minutes < 1:
        return "just now"
    if minutes < 60:
        return _count(minutes, "minute")
    if minutes < 1440:
        return _count(minutes // 60, "hour")
    if minutes < 10080:
        return _count(minutes // 1440, "day")
    return _count(minutes // 10080, "week")


def _minutes(ts, now) -> int | None:
    t, n = _dt(ts), _dt(now)
    return max(0, int((n - t).total_seconds() // 60)) if t and n else None


def plain_ago(ts, now) -> str:
    """How long before `now` a time was: "27 minutes ago", or "just now"; "" without a time."""
    m = _minutes(ts, now)
    if m is None:
        return ""
    w = plain_wait(m)
    return w if w == "just now" else w + " ago"


# ── the waiting items ─────────────────────────────────────────────────────────

def home_item(kind: str, *, label: str, title: str, detail: str, meta: str, since, now,
              actions: list[dict], project: dict | None = None, repo: str | None = None,
              from_: dict | None = None, id: str | None = None) -> dict:
    """One thing that waits for the person, in the shape every list of waiting items uses.
    `project` and `from_` are {id, name} (`from_` is {crew: True, name: "Your crew"} for the
    crew); `since` is when it began to wait. An action is a link (`href`) or something the page
    does (`do`, one of ACTIONS), never both. `id` is made from the kind, the project and the title
    unless the builder gives a stable one."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; one of: " + ", ".join(KINDS))
    acts = []
    for a in actions:
        if a.get("href") and a.get("do"):
            raise ValueError(f"action {a.get('label')!r} has both href and do; it is one or the other")
        if a.get("do") and a["do"] not in ACTIONS:
            raise ValueError(f"unknown action {a['do']!r}; one of: " + ", ".join(ACTIONS))
        acts.append({**a, "primary": bool(a.get("primary"))})
    pid = (project or {}).get("id") or ""
    key = hashlib.sha1(f"{title}\n{repo or ''}".encode()).hexdigest()[:10]
    return {"id": id or f"{kind}:{pid}:{key}", "kind": kind, "label": label, "title": title,
            "detail": detail, "meta": meta, "project": project, "repo": repo, "from": from_,
            "since": _iso(since), "waited": plain_wait(_minutes(since, now)), "actions": acts}


def waiting_items(store, fx, now) -> list[dict]:
    """Every builder's items, oldest first."""
    items = [i for build in WAITING_BUILDERS for i in build(store, fx, now)]
    items.sort(key=lambda i: _dt(i["since"]) or now)
    return items


# ── projects ──────────────────────────────────────────────────────────────────

def project_state(p: dict, tickets: list[dict], handover: dict | None, waiting: list[dict]) -> str:
    """building, waiting_on_you, not_started or ready_to_close, in this order of precedence."""
    prog = factory_mod.progress(tickets)
    if factory_mod.ready_to_close(p, tickets):
        return "ready_to_close"
    if prog["done"] == 0 and prog["doing"] == 0:
        return "not_started"
    mine = [w for w in waiting if (w.get("project") or {}).get("id") == p["id"]]
    if (mine and prog["doing"] == 0) or any(w["kind"] in ("permission", "question") for w in mine):
        return "waiting_on_you"
    return "building"


def _short(repo: str | None) -> str | None:
    return repo.rsplit("/", 1)[-1] if repo else None


def _current_milestone(milestones: list[dict], tickets: list[dict]) -> dict | None:
    """The first milestone with a ticket not done (canceled counts as done), as the project page
    reads it."""
    def done(t):
        return t["stage"] in ("merged", "shipped") or t["status"] in factory_mod.DONE_STATUSES
    for m in milestones:
        if any(not done(t) for t in tickets if t["milestone"] == m["id"]):
            return m
    return None


def project_row(p: dict, tickets: list[dict], handover: dict | None, waiting: list[dict],
                stations: list[dict], now, milestones: list[dict] | None = None) -> dict:
    """A project as the home page and the projects page list it. `milestones` is the project's
    milestones in order (store.milestones_by_project); without them the row has no milestone line."""
    state = project_state(p, tickets, handover, waiting)
    prog = factory_mod.progress(tickets)
    names = [_short(r.get("name")) for r in (handover or {}).get("repos") or []]
    names = [n for n in names if n]
    if not names:
        names = [_short(t.get("repo") or (t.get("pr") or {}).get("repo")) for t in tickets]
    repos = list(dict.fromkeys(n for n in names if n))
    builders = sum(1 for s in stations if s["role"] == "builder" and s["project"] == p["id"]
                   and s["state"] != "ended")
    at = _dt(factory_mod.activity_at(p.get("updated_at"), tickets))
    ago = plain_ago(at, now)
    milestones = milestones or []
    if state == "ready_to_close":
        milestone = "Ready to close"
    elif state == "not_started" and milestones:
        milestone = _count(len(milestones), "milestone")
    else:
        cur = _current_milestone(milestones, tickets)
        milestone = (f"Now on {cur['name']}" if cur
                     else _count(len(milestones), "milestone") if milestones else None)
    meta = [_count(builders, "builder")] if builders else []
    meta += [", ".join(repos)] if repos else []
    meta += ["active " + ago] if ago else []
    return {"id": p["id"], "name": p["name"], "goal": p.get("goal") or "", "state": state,
            "state_label": STATE_LABEL[state], "milestone": milestone,
            "tickets": {"merged": prog["done"], "blocked": prog["blocked"], "total": prog["total"]},
            "repos": repos, "builders": builders, "last_activity_at": _iso(at),
            "last_activity": ago or None, "meta": " · ".join(meta),
            "can_close": state == "ready_to_close"}


# ── the crew ──────────────────────────────────────────────────────────────────

def crew_member(station: dict, tickets_by_holder: dict[str, list[dict]], desk_open: int,
                now) -> dict:
    """One station as the crew card shows it. `tickets_by_holder` is {station name: its tickets}."""
    role, state = station["role"], station["state"]
    name = (f"Builder, {station.get('project_name')}" if role == "builder" and station.get("project_name")
            else ROLE_NAME.get(role) or station["name"])
    shown = "not heard" if state == "quiet" else state
    held = next((t for t in tickets_by_holder.get(station["name"], [])
                 if t.get("stage") in _WORKING_STAGES), None)
    if state == "working" and held:
        doing = f"Working on {held['title']}"
    elif state == "waiting" and station.get("state_reason"):
        doing = station["state_reason"]
    elif role == "orchestrator" and state == "idle" and not desk_open:
        doing = "Desk empty."
    else:
        doing = ROLE_LINE.get(role, "")
    since = station.get("last_at") if shown == "not heard" else (station.get("state_at")
                                                                 or station.get("last_at"))
    pct = ((station.get("context") or {}).get("pct"))
    pct = round(pct) if isinstance(pct, (int, float)) else None
    return {"station": station["name"], "role": role, "name": name, "state": shown,
            "since": plain_ago(since, now), "doing": doing, "memory_pct": pct,
            "memory": f"memory {pct}% full" if pct is not None else None}


def _how_far(settings: dict) -> dict:
    releases = settings.get("releaser") is not False
    autonomy = settings.get("autonomy") or "L3-review"
    text = ("Merges on its own after review and green CI" if autonomy == "L4-ship"
            else "Up to merge")
    return {"autonomy": autonomy, "releases": releases,
            "text": text if releases else text + ", no releases"}


def _crew(stations: list[dict], settings: dict, open_ids: dict, tickets: dict, desk_open: int,
          now) -> dict:
    orch = next((s for s in stations if s["role"] == "orchestrator"), None)
    running = bool(orch and orch["state"] != "ended")
    heard = _dt(orch["last_at"]) if running else None
    silent = _minutes(heard, now)
    if not running:
        summary = "Not running"
    elif silent is not None and silent > _STALE_MINUTES:
        summary = "Not heard from in " + plain_wait(silent)
    else:
        summary = ("Running, last heard " + plain_ago(heard, now)) if heard else "Running"
    by_holder: dict[str, list[dict]] = {}
    for ts in tickets.values():
        for t in ts:
            if t.get("holder"):
                by_holder.setdefault(t["holder"], []).append(t)
    keep = [s for s in stations if s["state"] != "ended" and (
        (s["role"] in _ALWAYS_ON and not (s["role"] == "releaser" and settings.get("releaser") is False))
        or (s["role"] == "builder" and s["project"] in open_ids))]
    keep.sort(key=lambda s: (_ALWAYS_ON.index(s["role"]) if s["role"] in _ALWAYS_ON else 9, s["name"]))
    return {"running": running, "heard_at": _iso(heard), "heard": plain_ago(heard, now) or None,
            "where": None, "summary": summary,
            "members": [crew_member(s, by_holder, desk_open, now) for s in keep],
            "how_far": _how_far(settings), "desk_open": desk_open}


def _headline(crew: dict, projects: list[dict], finished: int, working: int, waiting: list[dict],
              silent: int | None) -> str:
    if not projects and not finished:
        return "No projects yet. Start one and hand it to the crew."
    if not crew["running"]:
        part = "The crew is not running."
    elif silent is not None and silent > _STALE_MINUTES:
        part = f"The crew has not been heard from in {plain_wait(silent)}."
    elif working:
        part = f"The crew is working on {_count(working, 'ticket')}."
    elif crew["desk_open"]:
        part = f"The crew is idle, {_count(crew['desk_open'], 'question')} on its desk."
    else:
        part = "The crew is idle with nothing on its desk."
    if not waiting:
        return part + " Nothing waits for you."
    waited = "less than a minute" if waiting[0]["waited"] == "just now" else waiting[0]["waited"]
    if len(waiting) == 1:
        return part + f" 1 thing waits for you, for {waited}."
    return part + f" {len(waiting)} things wait for you, the oldest for {waited}."


# ── the plan and the tools ────────────────────────────────────────────────────

_LIMITS = {"session": "Current 5 hours", "weekly_all": "This week"}


def _resets(at: datetime | None, now: datetime) -> str | None:
    if not at:
        return None
    at, now = at.astimezone(timezone.utc), now.astimezone(timezone.utc)
    return at.strftime("%H:%M") if at.date() == now.date() else _DAYS[at.weekday()]


def _plan(store, now: datetime, names: dict) -> dict:
    plan = claude_usage.plan(store)
    limits = []
    for x in (plan or {}).get("limits") or []:
        if x["kind"] not in _LIMITS:
            continue
        at = None if x.get("reset") else claude_usage._when(x.get("resets_at"))
        pct = 0 if x.get("reset") else round(x.get("percent") or 0)
        resets = _resets(at, now)
        limits.append({"kind": x["kind"], "label": _LIMITS[x["kind"]], "pct": pct, "resets": resets,
                       "resets_at": _iso(at),
                       "text": f"{pct}% used" + (f" · resets {resets}" if resets else "")})
    shares = [f"{names[r['project']]} {round(r['share'] * 100)}%"
              for r in claude_usage.by_project(store, now - timedelta(days=7))
              if r["kind"] == "project" and r["project"] in names and (r["share"] or 0) >= 0.05]
    return {"known": plan is not None, "limits": limits,
            "share_text": "This week: " + ", ".join(shares) if shares else None}


# ── the page ──────────────────────────────────────────────────────────────────

def home(store, fx, now, credential_key: str = "open") -> dict:
    """Everything the home page shows, from one read of the projects, their tickets, the stations,
    the plan and the desk."""
    stations = fx.stations() if fx else []
    settings = fx.settings() if fx else dict(factory_mod.CREW_DEFAULTS)
    projects = [p for p in store.list_projects() if p.get("kind") == "software_factory"]
    open_ps = [p for p in projects if p["status"] != "done"]
    ids = [p["id"] for p in open_ps]
    tickets = store.tickets_by_project(ids)
    milestones = store.milestones_by_project(ids)
    desk_open = len(store.list_desk("open", None))
    waiting = waiting_items(store, fx, now)
    rows = []
    for p in open_ps:
        row = project_row(p, tickets[p["id"]], store.get_handover(p["id"]), waiting, stations, now,
                          milestones[p["id"]])
        rows.append(row)
    rows.sort(key=lambda r: (_STATE_ORDER[r["state"]],
                             -(_dt(r["last_activity_at"]) or datetime.fromtimestamp(0, timezone.utc)).timestamp()))
    crew = _crew(stations, settings, {p["id"] for p in open_ps}, tickets, desk_open, now)
    orch = next((s for s in stations if s["role"] == "orchestrator"), None)
    silent = _minutes(orch["last_at"], now) if orch and crew["running"] else None
    working = sum(factory_mod.progress(ts)["doing"] for ts in tickets.values())
    linear = linear_mod.public_connection(store)["connected"]
    return {"edition": "factory",
            "headline": _headline(crew, rows, len(projects) - len(open_ps), working, waiting, silent),
            "waiting_for_you": waiting, "waiting_total": len(waiting), "projects": rows,
            "finished_count": len(projects) - len(open_ps), "crew": crew,
            "plan": _plan(store, now, {p["id"]: p["name"] for p in projects}),
            "tools": [{"name": "GitHub", "connected": bool(store.list_github_credentials())},
                      {"name": "Linear", "connected": bool(linear)},
                      {"name": "Claude Code", "connected": bool(stations) or any(
                          c["connector"] == "claude_code" for c in store.list_catalog_sources())}],
            "now": now.isoformat()}


_cache: dict = {}
_cache_lock = threading.Lock()


def build(store, fx=None, request=None, now=None) -> dict:
    """GET /api/home: `home()` kept for 5 seconds per credential (the page polls every 15). A
    caller who passes `now` (a test) gets a fresh read."""
    if now is not None:
        return home(store, fx, now)
    cred = getattr(getattr(request, "state", None), "credential", None)
    key = (id(store), (cred or {}).get("name") or "open")
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    out = home(store, fx, now_utc(), key[1])
    with _cache_lock:
        _cache[key] = (time.monotonic(), out)
    return out
