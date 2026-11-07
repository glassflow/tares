# Tares enterprise licence, see ee/LICENSE
"""Grants that wait for someone else to agree (TR-922).

The grant kinds (team, project, personal) stay in core and work for one person: the owner is admin
and lead, so their grants take effect at once. What lives here is the waiting: a project grant
that reaches into a repo someone else owns waits for that owner, and one from a person who is
neither the project's lead nor an admin also waits for the lead (for an admin when the project has
none). Here is who it waits for (`waiting_for`), whether the caller is one of them
(`waits_for_me`), settling a grant nobody is left to wait for (`settle`), and the two routes where
a person says yes or no: `POST /api/grants/{gid}/consent` and `.../decline`. Each route answers
`licence_required` unless the licence allows ee, before it does anything with the request. Without
ee at all the same paths answer from `tares.team.FALLBACKS`. The MCP tool is in `mcp_grants.py`.

Without ee a grant that is already waiting stays waiting: nothing here runs, so nothing settles it,
it is not copied into the grants docs, and it can only be revoked (core).

`c` is the namespace `factory_api.team_reader()` builds: `store`, `h` (the host helpers),
`mirror_in`, `mirror_out`, `visible_grant` and `grants_view`.
"""
from __future__ import annotations

from fastapi import Body, Request

from tares import factory as factory_mod
from tares import licence
from tares.envelope import now_utc
from tares.factory_api import caller

__all__ = ["NEEDS_LICENCE", "pending_owners", "pending_lead", "waiting_for", "waits_for_me",
           "waiting_for_me", "settle", "settle_on_read", "grant_or_404", "register_routes"]

NEEDS_LICENCE = "Agreeing to someone else's grant needs an enterprise licence."


def pending_owners(c, areas, given_by, consents) -> list[dict]:
    """The owners of `areas` who have still to consent: not the giver, not already consented."""
    done = {x["person_id"] for x in consents}
    out: dict[str, dict] = {}
    for repo in areas:
        o = c.h.area_owner(repo)
        if o and o["id"] != given_by and o["id"] not in done:
            out[o["id"]] = {"id": o["id"], "name": o["name"]}
    return list(out.values())


def pending_lead(c, project, needs_lead, consents) -> list[dict]:
    """The lead who has still to consent, or {id: "admin"} when the project has no lead."""
    if not needs_lead or any(x.get("as") == "admin" for x in consents):
        return []
    lead = (c.store.get_project(project) or {}).get("lead_person_id") if project else None
    if not lead:
        return [{"id": "admin", "name": "an admin"}]
    if lead in {x["person_id"] for x in consents}:
        return []
    return [{"id": lead, "name": (c.store._person_refs({lead}).get(lead) or {}).get("name")}]


def waiting_for(c, g: dict) -> list[dict]:
    """The people `g` still waits for, as {id, name}. A grant that is not waiting waits for no one."""
    if g["state"] != "waiting":
        return []
    out = pending_owners(c, g["areas"], g["given_by"], g["consents"])
    have = {w["id"] for w in out}
    return out + [w for w in pending_lead(c, g["project"], g["needs_lead"], g["consents"])
                  if w["id"] not in have]


def waits_for_me(c, g: dict, request: Request) -> str | None:
    """How this caller may consent to g: "person" (they are named), "admin" (the grant waits
    for an admin and the credential has the admin scope), or None."""
    ids = {w["id"] for w in waiting_for(c, g)}
    person = c.h.current_person(request)
    if person and person["id"] in ids:
        return "person"
    if "admin" in ids and c.h.is_admin(request):
        return "admin"
    return None


def waiting_for_me(c, grants: list[dict], request: Request) -> list[dict]:
    """The project grants among `grants` that wait for the caller's consent."""
    return [g for g in grants if g["state"] == "waiting" and g["kind"] == "project"
            and waits_for_me(c, g, request)]


def settle(c, g: dict, by: str) -> dict:
    """A grant still waiting with nobody left to wait for (an owner removed, the repo given
    to the giver) takes effect now."""
    if g["state"] == "waiting" and not waiting_for(c, g):
        c.mirror_in(g, by)   # first: a failure leaves it waiting, to settle on the next try
        if not c.store.activate_waiting_grant(g["id"]):
            now = c.store.get_grant_row(g["id"])
            if now is not None and now["state"] == "active":
                return now   # another settle took it into effect first: its line stays
            # declined or revoked since it was read: it stays so, and its line comes back out
            c.mirror_out(g, by)
            c.h.err(ValueError(f"{g['id']} is no longer waiting"), 409)
        g = c.store.get_grant_row(g["id"])
    return g


def settle_on_read(c, g: dict, request: Request) -> dict:
    """Settle a grant the reader may see, as Tares and not as the reader. One that fails
    stays as it was and does not fail the read."""
    if g["state"] != "waiting" or not c.visible_grant(g, request):
        return g
    try:
        return settle(c, g, "tares")
    except Exception:
        return g


def grant_or_404(c, gid: str, request: Request) -> dict:
    """The grant, or 404. Someone a grant waits for may see it to consent, even on a private
    project they are not in (they get its words and areas, not the project)."""
    g = c.store.get_grant_row(gid)
    if g is None or not (c.visible_grant(g, request)
                         or (g["kind"] == "project" and waits_for_me(c, g, request))):
        c.h.err(KeyError(f"unknown grant {gid!r}"), 404)
    return g


def register_routes(app, host) -> None:
    store = host.store

    def need_licence():
        if not licence.ee_allowed():
            host.licence_required(NEEDS_LICENCE)

    def deny(request: Request, action: str) -> None:
        why = factory_mod.may(caller(request), action)
        if why:
            host.err(PermissionError(why), 403)

    def console_via(request: Request) -> str | None:
        """"console" for a person acting on the page (no station header), else None."""
        return None if caller(request)["station"] else "console"

    @app.post("/api/grants/{gid}/consent")
    def consent_grant(gid: str, request: Request):
        """The owner of a repo the grant names, or the project's lead (an admin when it has none)
        when the giver is neither, says yes. Only someone it is waiting for may, even on a private
        project they are not in; the grant takes effect when the last of them has."""
        need_licence()
        c = host.team_reader()
        deny(request, "grants")
        g = grant_or_404(c, gid, request)
        person = host.current_person(request)
        if g["state"] != "waiting":
            host.err(ValueError(f"{g['id']} is {g['state']}; there is nothing to consent to"), 409)
        how = waits_for_me(c, g, request)
        if how is None:
            names = ", ".join(w["name"] or w["id"] for w in waiting_for(c, g))
            host.err(PermissionError(f"{g['id']} waits for {names or 'no one'}; this key is not "
                                     "one of them"), 403)
        entry = {"person_id": person["id"] if person else "system", "at": now_utc().isoformat()}
        if how == "admin":
            entry["as"] = "admin"
        if console_via(request):
            entry["via"] = "console"
        consents = g["consents"] + [entry]
        # only while it still waits: a decline that came since the read must stay a decline
        if not store.consent_waiting_grant(g["id"], consents):
            host.err(ValueError(f"{g['id']} is no longer waiting; there is nothing to consent to"), 409)
        g = settle(c, {**g, "consents": consents}, host.by(request, None))
        if g["state"] == "waiting":
            g = store.get_grant_row(g["id"])
        return {"grant": c.grants_view([g])[0]}

    @app.post("/api/grants/{gid}/decline")
    def decline_grant(gid: str, request: Request, body: dict = Body(default={})):
        """{reason?}: someone a waiting grant waits for says no (TR-740). The grant ends as a
        revoked one, so every reader keeps working, and the consents list keeps who declined, when,
        from where and why. Only someone it is waiting for may; a grant that is not waiting
        answers 409."""
        need_licence()
        c = host.team_reader()
        deny(request, "grants")
        g = grant_or_404(c, gid, request)
        person = host.current_person(request)
        if g["state"] != "waiting":
            host.err(ValueError(f"{g['id']} is {g['state']}; there is nothing to decline"), 409)
        how = waits_for_me(c, g, request)
        if how is None:
            names = ", ".join(w["name"] or w["id"] for w in waiting_for(c, g))
            host.err(PermissionError(f"{g['id']} waits for {names or 'no one'}; this key is not "
                                     "one of them"), 403)
        reason = str(body.get("reason") or "").strip()
        if len(reason) > 1000:
            host.err(ValueError("keep the reason to 1000 characters"))
        entry = {"person_id": person["id"] if person else "system", "at": now_utc().isoformat(),
                 "declined": True}
        if reason:
            entry["reason"] = reason
        if how == "admin":
            entry["as"] = "admin"
        if console_via(request):
            entry["via"] = "console"
        # a waiting grant was never in effect, so there is nothing to take out of the grants doc
        if not store.decline_waiting_grant(g["id"], g["consents"] + [entry]):
            host.err(ValueError(f"{g['id']} is no longer waiting; there is nothing to decline"), 409)
        return {"grant": c.grants_view([store.get_grant_row(g["id"])])[0]}
