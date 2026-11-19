"""The factory Projects list (TR-733): GET /api/factory/projects.

Every software-factory project is one row, from one read: the projects, all their tickets in one
query, the handovers and the crew's stations. A project the cell made for another kind of use (the
Default, or one made before the edition was set) is left out. The rows are the home page's rows
(`factory_home.project_row`) with the meta line worded for this page (builders, how long ago, pull
requests waiting), the full repo names in `repo_names` and `can_close`, true for a project whose
tickets are all merged. A finished project is a row too, in state "done".

The page filters by state in the browser, so the answer carries all the rows and the count of each
state. Closing a project goes through the route that exists (`POST /api/projects/{id}/finish`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request

from ..envelope import now_utc
from .. import factory_owners as owners_mod
from ..factory_home import (_STATE_ORDER, _caller, _count, _dt, _short, for_caller, plain_ago,
                            project_row, caller_owns, caller_waiting, team_of)

# the order of the list: what needs you first, kept for later and finished last
ORDER = {**_STATE_ORDER, "later": 4, "done": 5}
DONE_LABEL = "Done"
LATER_LABEL = "Later"
STATES = ("building", "waiting_on_you", "not_started", "ready_to_close", "later", "done")
_EPOCH = datetime.fromtimestamp(0, timezone.utc)


def plain_date(ts, now) -> str:
    """"20 Oct", and the year after it for a day in another year; "" without a time."""
    t, n = _dt(ts), _dt(now)
    if t is None:
        return ""
    day = f"{t.day} {t.strftime('%b')}"
    return day if n is None or t.year == n.year else f"{day} {t.year}"


def _full_repos(handover: dict | None, tickets: list[dict]) -> list[str]:
    names = [str(r.get("name") or "") for r in (handover or {}).get("repos") or []]
    if not any(names):
        names = [str(t.get("repo") or (t.get("pr") or {}).get("repo") or "") for t in tickets]
    return list(dict.fromkeys(n for n in names if n))


def row_for(p: dict, tickets: list[dict], handover: dict | None, waiting: list[dict],
            stations: list[dict], now, milestones: list[dict]) -> dict:
    """One project as the page lists it."""
    row = project_row(p, tickets, handover, waiting, stations, now, milestones)
    if p.get("status") == "done":
        finished = _dt(((handover or {}).get("finished") or {}).get("at")) or _dt(p.get("updated_at"))
        row["state"], row["state_label"], row["can_close"] = "done", DONE_LABEL, False
        row["milestone"] = "Finished " + plain_date(finished, now) if finished else "Finished"
        row["last_activity_at"] = finished.isoformat() if finished else row["last_activity_at"]
        row["last_activity"] = plain_ago(finished, now) or None
        row["builders"] = 0
    elif p.get("status") == "later":
        row["state"], row["state_label"], row["can_close"] = "later", LATER_LABEL, False
    repos = _full_repos(handover, tickets)
    builders = row["builders"]
    state = row["state"]
    ago = row["last_activity"]
    parts: list[str] = []
    if state == "done":
        pass
    elif state == "later":
        parts.append("Kept for later")
    elif builders:
        parts.append(_count(builders, "builder"))
    else:
        parts.append("No builders yet" if state == "not_started" else "No builders working")
    if state in ("building", "waiting_on_you") and ago:
        parts.append("Active " + ago)
    elif state == "not_started" and ago:
        parts.append("Written " + ago)
    elif state == "ready_to_close" and ago:
        parts.append("Merged " + ago)
    for w in waiting if state != "done" else ():
        if w["kind"] == "prs_to_review" and (w.get("project") or {}).get("id") == p["id"]:
            parts.append(w["label"])
    if state == "ready_to_close":
        row["milestone"] = "All milestones done"
    # the repos are the person's own names, so they travel apart from the words around them
    row["repo_names"] = repos
    row["meta"] = " · ".join(parts)
    return row


def done_row_for(p: dict, facts: dict, handover: dict | None, now) -> dict | None:
    """The row `row_for` gives a finished project, from `store.done_ticket_facts` instead of its
    tickets: the same keys in the same order. None when the facts cannot give it (the counts are
    not exact, a repo would come from a pull request, or no finish time exists at all), and the
    caller then reads that project's tickets."""
    finished = _dt(((handover or {}).get("finished") or {}).get("at")) or _dt(p.get("updated_at"))
    if not facts["exact"] or finished is None:
        return None
    full = [str(r.get("name") or "") for r in (handover or {}).get("repos") or []]
    short = [n for n in (_short(r.get("name")) for r in (handover or {}).get("repos") or []) if n]
    if (not short or not any(full)) and facts["repo_from_pr"]:
        return None
    repos = list(dict.fromkeys(short or [n for n in (_short(r) for r in facts["repos"]) if n]))
    names = list(dict.fromkeys(n for n in full if n)) if any(full) else list(facts["repos"])
    return {"id": p["id"], "name": p["name"], "icon": p.get("icon"), "goal": p.get("goal") or "",
            "state": "done", "state_label": DONE_LABEL,
            "milestone": "Finished " + plain_date(finished, now),
            "tickets": {"merged": facts["merged"], "blocked": facts["blocked"],
                        "total": facts["total"]},
            "repos": repos, "builders": 0, "last_activity_at": finished.isoformat(),
            "last_activity": plain_ago(finished, now) or None, "meta": "", "can_close": False,
            "repo_names": names}


def register(app, store, h, ctx):
    @app.get("/api/factory/projects")
    def factory_projects(request: Request):
        """{projects: [row], counts: {all, building, waiting_on_you, not_started, ready_to_close,
        later, done}, now}. Sorted by state (waiting on you, building, not started, ready to close,
        later, done),
        newest activity first within a state."""
        now = now_utc()
        fx = ctx.fx
        stations = fx.stations() if fx else []
        hidden = h.hidden_ids()
        projects = store.list_projects(kind="software_factory", exclude_ids=hidden)
        ids = [p["id"] for p in projects]
        # what waits for the caller, as their home has it (never another person's items)
        fxc = for_caller(fx, request)
        home = team_of(fxc, request, now)   # built once: the waiting list and the team parts read it
        handovers = store.handovers_by_project(ids)
        # A finished project's row needs only counts, not its tickets. On a team cell the seam
        # (team_projects) reads every row's tickets, done ones too, so they load as before.
        done_ids = [] if home else [p["id"] for p in projects if p.get("status") == "done"]
        facts = store.done_ticket_facts(done_ids)
        done_rows = {p["id"]: done_row_for(p, facts[p["id"]], handovers.get(p["id"]), now)
                     for p in projects if p["id"] in facts}
        need = [i for i in ids if done_rows.get(i) is None]
        tickets = store.tickets_by_project(need)
        milestones = store.milestones_by_project(need)
        waiting = caller_waiting(store, fxc, now, request, hidden, home)
        rows = [done_rows[p["id"]] if done_rows.get(p["id"]) is not None else
                row_for(p, tickets[p["id"]], handovers.get(p["id"]), waiting, stations, now,
                        milestones[p["id"]]) for p in projects]
        # Close is for whoever owns the project's work (factory_owners)
        owners = owners_mod.Owners(store)
        who = _caller(store, fxc, request)
        by_id = {p["id"]: p for p in projects}
        for r in rows:
            r["can_close"] = r["can_close"] and caller_owns(store, fxc, request, by_id[r["id"]],
                                                            owners, who)
        rows.sort(key=lambda r: (ORDER[r["state"]],
                                 -(_dt(r["last_activity_at"]) or _EPOCH).timestamp()))
        counts = {"all": len(rows), **{s: sum(1 for r in rows if r["state"] == s) for s in STATES}}
        body = {"projects": rows, "counts": counts, "now": now.isoformat()}
        team = _team(fx, request, projects, tickets, rows, now, home)
        if team:
            body.update(team)
        return body


def _team(fx, request, projects, tickets, rows, now, home: dict | None = None) -> dict:
    """What a cell of more than one person adds (TR-1277), or {} for a cell of one and a cell with no
    licence: each row gains its lead, members, scope, the person's tickets in words and `mine` (the
    person leads it, holds a ticket in it, or something in it waits for them), and `team` holds the
    counts of the two views, a stub for each open private project the caller is not on, and the one
    line about them. The rows and the stubs come from the seam Home used (fx.team_projects), asked
    for the Team view; the page cuts Mine from it in the browser, so switching makes no call.
    `mine` reads what waits for the caller's person (fx.team_home), not the cell's list."""
    if fx is None or not hasattr(fx, "team_home") or not hasattr(fx, "team_projects"):
        return {}
    if home is None:
        home = fx.team_home(request, now)
    if not home:
        return {}
    keys = fx.team_projects(request, projects, tickets, rows, "team", now, home["waiting_for_you"])
    if not keys:
        return {}
    listed = keys["projects"]
    team = {"counts": keys["counts"], "hidden": [p for p in listed if p.get("hidden")]}
    if keys.get("hidden_summary"):
        team["hidden_summary"] = keys["hidden_summary"]
    return {"projects": [p for p in listed if not p.get("hidden")], "team": team}
