"""The factory Tickets list (TR-806): GET /api/factory/tickets.

Every ticket of every software-factory project, from one read: the projects and their tickets in
two queries, the milestones, the crew's stations. The page filters on the server, so the answer
is one page of tickets plus what the filters need: how many tickets sit in each state (`counts`,
for the project, milestone and search chosen, whatever the state), the projects and milestones
to choose from, and how many canceled tickets are hidden.

Query: `project` (an id), `milestone` (an id), `state` ("to do", "in progress", "in review",
"changes asked", "blocked", "merged", "canceled" or "moving", which is the three that move now:
in progress, in review and changes asked), `q` (text in the ref or the title), `sort` ("new",
"old" or "ref"), `limit` (25 by default, 200 at most) and `offset`. Canceled tickets show only when
`state` is "canceled". The Board and Swimlanes layouts ask for `limit=200` and cap each column
themselves.

Every string is worded for people, as on the project page (`factory_project`): a state is "in
review", who works on a ticket is "Builder for M2", never the name of a station, a time is "3 days
ago". The crew's folders stay on the crew's machine: a title that names one shows the repo.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import HTTPException, Request

from .. import factory as factory_mod
from ..envelope import now_utc
from ..factory_home import _dt, for_caller, plain_ago, waiting_items
from .factory_project import _pr_out, _review_out, station_roles, ticket_state, who_words
from .factory_projects import ORDER, row_for

# the states a person can pick, in the order the page lists them, with the key each counts under
STATES = ("to do", "in progress", "in review", "changes asked", "blocked", "merged", "canceled")
KEYS = {"to do": "to_do", "in progress": "in_progress", "in review": "in_review",
        "changes asked": "changes_asked", "blocked": "blocked", "merged": "merged",
        "canceled": "canceled"}
MOVING = ("in progress", "in review", "changes asked")
SORTS = ("new", "old", "ref")
DEFAULT_LIMIT, MAX_LIMIT = 25, 200
NOT_TAKEN = "Not picked up yet"
_EPOCH = datetime.fromtimestamp(0, timezone.utc)
_NUM = re.compile(r"(\d+)\s*$")


def _state_param(raw: str | None) -> str | None:
    """"in-progress", "in_progress" and "In progress" all mean "in progress"; None for no state."""
    if raw is None or not raw.strip():
        return None
    s = re.sub(r"[-_\s]+", " ", raw.strip().lower())
    if s == "moving" or s in STATES:
        return s
    raise HTTPException(400, "state is one of: " + ", ".join(STATES) + ", moving")


def _ref_number(ref: str) -> int:
    m = _NUM.search(ref)
    return int(m.group(1)) if m else 0


def register(app, store, h, ctx):
    @app.get("/api/factory/tickets")
    def factory_tickets(project: str | None = None, milestone: str | None = None,
                        state: str | None = None, q: str | None = None, sort: str = "new",
                        limit: int = DEFAULT_LIMIT, offset: int = 0, *, request: Request):
        """{tickets, total, offset, limit, more, counts, projects, milestones, hidden_canceled,
        now}. See the module note for the query."""
        want = _state_param(state)
        project, milestone = project or None, milestone or None
        if sort not in SORTS:
            raise HTTPException(400, "sort is one of: " + ", ".join(SORTS))
        if not 1 <= limit <= MAX_LIMIT:
            raise HTTPException(400, f"limit is 1 to {MAX_LIMIT}")
        if offset < 0:
            raise HTTPException(400, "offset is 0 or more")
        now = now_utc()
        fx = ctx.fx
        all_p = [p for p in store.list_projects() if p.get("kind") == "software_factory"]
        # a draft opens at its setup, not at a page with tickets
        projects = [p for p in all_p if p.get("status") != "draft"]
        if project is not None and project not in {p["id"] for p in projects}:
            raise HTTPException(404, f"no factory project {project!r}")
        ids = [p["id"] for p in projects]
        tickets_of = store.tickets_by_project(ids)
        milestones_of = store.milestones_by_project(ids)
        try:
            station_rows = store.station_sessions(h.cc_source())
        except Exception:
            station_rows = []
        roles = station_roles(station_rows)

        # the projects in the order of the Projects page: what needs you first, finished last
        waiting = waiting_items(store, for_caller(fx, request), now)
        stations = fx.stations() if fx else []
        ranked = []
        for p in projects:
            r = row_for(p, tickets_of[p["id"]], store.get_handover(p["id"]), waiting, stations,
                        now, milestones_of[p["id"]])
            ranked.append(((ORDER[r["state"]],
                            -(_dt(r["last_activity_at"]) or _EPOCH).timestamp()), p))
        ranked.sort(key=lambda x: x[0])
        projects = [p for _, p in ranked]

        def cleaner(p):
            # the crew's folders stay on the crew's machine: a title that names one shows the repo
            hidden = {}
            for r in (store.get_handover(p["id"]) or {}).get("repos") or []:
                base = (r.get("path") or "").rstrip("/")
                if base:
                    hidden[base] = r.get("name") or base.rsplit("/", 1)[-1] or "the repo"

            def clean(text):
                out = str(text or "")
                for path in sorted(hidden, key=len, reverse=True):
                    out = out.replace(path, hidden[path])
                return out
            return clean

        needle = (q or "").strip().lower()
        rows = []          # (sort time, ticket row) of every ticket the filters but the state keep
        counts = {"total": 0, "moving": 0, **{k: 0 for k in KEYS.values()}}
        for p in projects:
            if project is not None and p["id"] != project:
                continue
            tickets = tickets_of[p["id"]]
            rd = factory_mod.readiness(tickets)
            numbers = {m["id"]: f"M{i + 1}" for i, m in enumerate(milestones_of[p["id"]])}
            names = {m["id"]: m["name"] for m in milestones_of[p["id"]]}
            clean = cleaner(p)

            for t in tickets:
                if milestone is not None and t.get("milestone") != milestone:
                    continue
                ref = factory_mod.label(t)
                if needle and needle not in ref.lower() and needle not in clean(t["title"]).lower():
                    continue
                blocked_by = [] if factory_mod.finished(t) else rd[t["id"]]["blocked_by"]
                st = ticket_state(t, blocked_by)
                counts[KEYS[st]] += 1
                if st != "canceled":
                    counts["total"] += 1
                if st in MOVING:
                    counts["moving"] += 1
                done = factory_mod.finished(t)
                ms = t.get("milestone")
                rows.append((st, t, {
                    "ref": ref, "title": clean(t["title"]), "state": st, "state_key": KEYS[st],
                    "project": {"id": p["id"], "name": p["name"]},
                    "milestone": ({"id": ms, "number": numbers.get(ms), "name": clean(names.get(ms))}
                                  if ms in numbers else None),
                    "who": None if done else (who_words(t.get("holder"), roles, numbers.get(ms))
                                              or NOT_TAKEN),
                    "pr": _pr_out(t), "blocked_by": blocked_by,
                    "changed": plain_ago(t.get("updated_at"), now) or None,
                    "changed_at": _iso(t.get("updated_at")),
                    "href": f"/projects/{quote(p['id'], safe='')}/tickets/{quote(ref, safe='')}"}))

        def shown(st: str) -> bool:
            if want is None:
                return st != "canceled"
            return st in MOVING if want == "moving" else st == want

        picked = [r for r in rows if shown(r[0])]
        stamp = lambda r: _dt(r[1].get("updated_at")) or _EPOCH
        if sort == "old":
            picked.sort(key=stamp)
        elif sort == "ref":
            picked.sort(key=lambda r: (r[2]["project"]["name"].lower(), _ref_number(r[2]["ref"])))
        else:
            picked.sort(key=stamp, reverse=True)
        page = picked[offset:offset + limit]
        reviews = store.last_reviews([r[1]["id"] for r in page])
        out = []
        for _, t, row in page:
            row["review"] = _review_out(reviews.get(t["id"]), t)
            out.append(row)

        return {"tickets": out, "total": len(picked), "offset": offset, "limit": limit,
                "more": offset + len(out) < len(picked), "counts": counts,
                "projects": [{"id": p["id"], "name": p["name"],
                              "count": sum(1 for t in tickets_of[p["id"]]
                                           if t.get("status") != "canceled")}
                             for p in projects],
                "milestones": [{"id": m["id"], "project": p["id"], "number": f"M{i + 1}",
                                "name": cleaner(p)(m["name"])}
                               for p in projects for i, m in enumerate(milestones_of[p["id"]])
                               if project is None or p["id"] == project],
                "hidden_canceled": counts["canceled"] if want != "canceled" else 0,
                "now": now.isoformat()}


def _iso(ts) -> str | None:
    t = _dt(ts)
    return t.isoformat() if t else None
