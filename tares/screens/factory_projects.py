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
from ..factory_home import (_STATE_ORDER, _count, _dt, for_caller, plain_ago, project_row,
                            waiting_items)

# the order of the list: what needs you first, finished last
ORDER = {**_STATE_ORDER, "done": 4}
DONE_LABEL = "Done"
STATES = ("building", "waiting_on_you", "not_started", "ready_to_close", "done")
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
    repos = _full_repos(handover, tickets)
    builders = row["builders"]
    state = row["state"]
    ago = row["last_activity"]
    parts: list[str] = []
    if state == "done":
        pass
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


def register(app, store, h, ctx):
    @app.get("/api/factory/projects")
    def factory_projects(request: Request):
        """{projects: [row], counts: {all, building, waiting_on_you, not_started, ready_to_close,
        done}, now}. Sorted by state (waiting on you, building, not started, ready to close, done),
        newest activity first within a state."""
        now = now_utc()
        fx = ctx.fx
        stations = fx.stations() if fx else []
        hidden = h.hidden_ids()
        projects = [p for p in store.list_projects()
                    if p.get("kind") == "software_factory" and p["id"] not in hidden]
        ids = [p["id"] for p in projects]
        tickets = store.tickets_by_project(ids)
        milestones = store.milestones_by_project(ids)
        waiting = waiting_items(store, for_caller(fx, request), now, hidden)
        rows = [row_for(p, tickets[p["id"]], store.get_handover(p["id"]), waiting, stations, now,
                        milestones[p["id"]]) for p in projects]
        rows.sort(key=lambda r: (ORDER[r["state"]],
                                 -(_dt(r["last_activity_at"]) or _EPOCH).timestamp()))
        counts = {"all": len(rows), **{s: sum(1 for r in rows if r["state"] == s) for s in STATES}}
        return {"projects": rows, "counts": counts, "now": now.isoformat()}
