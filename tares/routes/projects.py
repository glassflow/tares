"""A project's row and lifecycle (tares/factory_projects.py) over the API: list, create, read,
change, pause, resume, delete, its timeline and summary."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import factory as factory_mod
from .. import factory_home, request_memo, timeline
from ..deps import Deps
from ..factory_projects import ProjectError


class ProjectIn(BaseModel):
    # `template`, `params` and `objects` are accepted and ignored: a project has no template
    # since Tares 2.0 (TR-1812), and older clients still send them
    name: str = ""
    goal: str | None = None       # one line, at most 200 characters
    kind: str | None = None       # "software_factory" for a project a spec session makes
    icon: str | None = None       # one emoji shown beside the name; none = no icon


class ProjectUpdate(BaseModel):
    name: str = ""         # blank = unchanged
    # the project's goal; left out = unchanged, "" or null = cleared
    goal: str | None = None
    # the project's icon, one emoji; left out = unchanged, "" or null = removed
    icon: str | None = None


def router(deps: Deps) -> APIRouter:
    r = APIRouter()
    store = deps.store
    projects = deps.projects
    _err = deps.err
    _current_person = deps.current_person
    _pk = deps.project_key
    _can_see = deps.can_see
    _hidden_project = deps.hidden_project
    _project_or_404 = deps.project_or_404

    # ── management API: projects (tares/factory_projects.py) ─────────────────
    def _uc_err(e: Exception):
        if isinstance(e, KeyError):
            _err(e, 404)
        if isinstance(e, (ProjectError, ValueError)):
            _err(e, 400)
        raise e

    def _project_card(uid: str) -> dict:
        """What a project key is told about its project: who it is and what it is made of."""
        p = store.get_project(uid) or {}
        return {"id": uid, "name": p.get("name"), "goal": p.get("goal"),
                "status": p.get("status"), "created_at": p.get("created_at"),
                "skills": [s["name"] for s in store.list_skills(uid)]}

    @r.get("/api/projects")
    @request_memo.page
    def list_projects(request: Request):
        pk = _pk(request)
        if pk:
            return {"projects": [_project_card(pk)]}
        # a software factory project carries its tickets' progress, for the list's ring, and
        # `activity_at`, when it last moved (its newest ticket counts), for the Updated column;
        # every factory project's tickets come in one query (TR-659)
        # a private project the caller is not on is listed as its name and lead and nothing else
        # (an admin hands its lead over from this row); everything else about it is unknown
        cards = projects.list()
        tickets = store.tickets_by_project([p["id"] for p in cards
                                            if p.get("kind") == "software_factory"
                                            and _can_see(p)])

        def card(p: dict) -> dict:
            if not _can_see(p):
                return _hidden_project(p)
            if p.get("kind") != "software_factory":
                return {**p, "activity_at": p["updated_at"], "ready_to_close": False}
            ts = tickets[p["id"]]
            return {**p, "progress": factory_mod.progress(ts),
                    "activity_at": factory_mod.activity_at(p["updated_at"], ts),
                    "ready_to_close": factory_mod.ready_to_close(p, ts)}
        return {"projects": [card(p) for p in cards]}

    @r.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn, request: Request):
        # the person who makes a project leads it; the system credential is no person, so a
        # project it makes has no lead until an admin claims it
        person = _current_person(request)
        try:
            return projects.create(body.name, goal=body.goal, icon=body.icon, kind=body.kind,
                                   lead_person_id=person["id"] if person else None)
        except Exception as e:
            _uc_err(e)

    @r.get("/api/projects/{uid}")
    def get_project(uid: str, request: Request):
        if _pk(request):
            return _project_card(uid)   # the guard already held it to the key's own project
        _project_or_404(uid)
        inst = projects.get(uid)
        if inst is None:
            _err(KeyError(f"unknown project {uid!r}"), 404)
        return inst

    @r.put("/api/projects/{uid}")
    def update_project(uid: str, body: ProjectUpdate):
        """Change the name, the goal or the icon; what the body leaves out stays as it is."""
        _project_or_404(uid)
        given = body.model_fields_set
        kw = {k: getattr(body, k) for k in ("goal", "icon") if k in given}
        try:
            out = projects.update(uid, name=body.name, **kw)
        except Exception as e:
            _uc_err(e)
        if body.name.strip() or "icon" in given:
            factory_home.clear_cache()   # Home keeps its answer for a few seconds
        return out

    @r.delete("/api/projects/{uid}")
    def delete_project(uid: str):
        _project_or_404(uid)
        try:
            out = projects.delete(uid)
        except Exception as e:
            _uc_err(e)
        factory_home.clear_cache()
        return out

    @r.post("/api/projects/{uid}/pause")
    def pause_project(uid: str):
        _project_or_404(uid)
        try:
            return projects.pause(uid)
        except Exception as e:
            _uc_err(e)

    @r.post("/api/projects/{uid}/resume")
    def resume_project(uid: str):
        _project_or_404(uid)
        try:
            return projects.resume(uid)
        except Exception as e:
            _uc_err(e)

    @r.get("/api/projects/{uid}/timeline")
    async def project_timeline(request: Request, uid: str, limit: int = 50, before: str = "",
                               trigger: str = "", agent: str = "", outcome: str = "",
                               entity: str = ""):
        """What happened in the project, newest first (TR-331): the hand-overs pushed through
        without every sign-off (`events`). `threads` stays, empty, since triggers and agents went.
        Page with `before` = the previous page's `next_before`; a `before` without a zone is UTC.
        Any of `trigger`, `agent`, `outcome`, `entity` filters threads, so `events` are left out."""
        _project_or_404(uid)
        if projects.get(uid) is None:
            _err(KeyError(f"unknown project {uid!r}"), 404)
        at = None
        if before:
            try:
                at = datetime.fromisoformat(before.replace("Z", "+00:00"))
                if at.tzinfo is None:   # no zone means UTC, not the host's time
                    at = at.replace(tzinfo=timezone.utc)
            except ValueError:
                _err(ValueError("before must be an ISO timestamp, as next_before gives it"))
        if outcome and outcome not in timeline.OUTCOMES:
            _err(ValueError(f"outcome must be one of {', '.join(timeline.OUTCOMES)}"))
        out = await asyncio.to_thread(
            timeline.project_timeline, store, uid, limit=limit, before=at, trigger=trigger,
            agent=agent, outcome=outcome, entity=entity, scheduled=set())
        filtered = bool(trigger or agent or outcome or entity)
        events = [] if filtered else timeline.override_events(
            store.list_handover_overrides(uid), at, out.get("next_before"))
        if events:
            # every hand-over the lead pushed through without every sign-off (TR-636), on the page
            # whose window holds its time, so across pages none repeats and none is lost
            out["events"] = events
        return out

    @r.get("/api/projects/{uid}/summary")
    def project_summary(uid: str):
        _project_or_404(uid)
        try:
            out = projects.summary(uid)
        except Exception as e:
            _uc_err(e)
        factory = out.get("kind") == "software_factory"
        out["ready_to_close"] = factory and factory_mod.ready_to_close(out, store.list_tickets(uid))
        return out

    return r
