# Tares enterprise licence, see ee/LICENSE
"""Who leads a project, whether it is private, and who is on it (TR-921).

The routes that change a project's lead, scope or members: `PATCH /api/projects/{uid}` and the
`/members` routes. Each one answers `licence_required` unless the licence allows ee, before it does
anything with the request. Without ee at all the same paths answer from `tares.team.FALLBACKS`.

The lead is still set when a project is made (core, `store.set_project_lead`), and what a person may
see is core too (`_can_see`): both work the same with or without a licence. The MCP tool is in
`mcp_access.py`.
"""
from __future__ import annotations

from fastapi import Body, HTTPException, Request

from tares import licence

__all__ = ["NEEDS_LICENCE", "register_routes"]

NEEDS_LICENCE = "Changing a project's lead, privacy or members needs an enterprise licence."


def register_routes(app, host) -> None:
    store = host.store

    def need_licence():
        if not licence.ee_allowed():
            host.licence_required(NEEDS_LICENCE)

    @app.patch("/api/projects/{uid}")
    def set_project_access(uid: str, request: Request, body: dict = Body(...)):
        """{lead_person_id?, scope?}: who leads the project and whether it is `team` or
        `private`. The lead changes both. An admin (or the system credential) changes the lead
        only, which is also how a project with no lead is claimed. Nobody else changes either.
        Answers with the project. A private project the caller is not on is unknown to them,
        except that an admin (the admin scope, which the guard already requires for this route)
        may still hand it to a lead, so it is never left without one."""
        need_licence()
        p = store.get_project(uid)
        body = body or {}
        hand_over_only = host.is_admin(request) and "lead_person_id" in body and "scope" not in body
        if p is None or not (host.can_see(p) or hand_over_only):
            host.err(KeyError(f"unknown project {uid!r}"), 404)
        want_lead, want_scope = "lead_person_id" in body, "scope" in body
        if not (want_lead or want_scope):
            host.err(ValueError("say lead_person_id, scope or both"))
        person = host.current_person(request)
        is_lead = bool(person and p["lead_person_id"] == person["id"])
        if not is_lead and (want_scope or not host.is_admin(request)):
            raise HTTPException(status_code=403, detail="only the project lead can change this")
        if want_scope and body["scope"] not in ("team", "private"):
            host.err(ValueError("scope is team or private"))
        if want_lead:
            lead = str(body["lead_person_id"] or "").strip()
            if not lead:
                host.err(ValueError("lead_person_id must be a person id"))
            if store.get_person(lead) is None:
                host.err(KeyError(f"no person {lead!r}; GET /api/people lists them"), 404)
            old_lead = p["lead_person_id"]
            store.set_project_lead(uid, lead)
            if old_lead and old_lead != lead:   # the one who handed it over stays on it
                store.add_project_member(uid, old_lead, person["id"] if person else "system")
        if want_scope:
            store.set_project_scope(uid, body["scope"])
        out = host.projects().get(uid)
        return out if host.can_see(store.get_project(uid)) else host.hidden_project(out)

    def _lead_or_system(request, uid: str) -> dict:
        """The project, when this caller may change who is on it: its lead, or the system
        credential (an admin who is not the lead may not)."""
        p = host.project_or_404(uid)
        person = host.current_person(request)
        if not (person and p["lead_person_id"] == person["id"]) and not host.is_system(request):
            raise HTTPException(status_code=403, detail="only the project lead can change this")
        return p

    @app.post("/api/projects/{uid}/members")
    def add_project_member(uid: str, request: Request, body: dict = Body(...)):
        """{person_id}: put a person on the project. Adding someone already on it changes
        nothing. Answers with the project."""
        need_licence()
        p = _lead_or_system(request, uid)
        pid = str((body or {}).get("person_id") or "").strip()
        if not pid:
            host.err(ValueError("person_id is required"))
        if store.get_person(pid) is None:
            host.err(KeyError(f"no person {pid!r}; GET /api/people lists them"), 404)
        if pid != p["lead_person_id"]:   # the lead is a member already
            by = host.current_person(request)
            store.add_project_member(uid, pid, by["id"] if by else "system")
        return host.projects().get(uid)

    @app.delete("/api/projects/{uid}/members/{person_id}")
    def remove_project_member(uid: str, person_id: str, request: Request):
        """Take a person off the project. The lead cannot be taken off: hand the project to
        someone else first. Answers with the project."""
        need_licence()
        p = _lead_or_system(request, uid)
        if person_id == p["lead_person_id"]:
            host.err(ValueError("the lead is always a member; make someone else the lead first"), 409)
        store.remove_project_member(uid, person_id)
        return host.projects().get(uid)
