"""The factory People page (TR-1011): GET and POST /api/factory/people.

Who is on the cell, and the one way to add someone. A row is a person: name, email, admin or
not, the projects they lead, the repos they own (areas), and whether they have signed in (a key
bound to them has been used). A private project the caller is not on is left out of a lead's
list, as everywhere else.

Adding a person:
- On a self-hosted cell, POST /api/factory/people creates the person (as POST /api/people does).
  They sign in the way the cell's people do today.
- On Tares Cloud (TARES_WORKSPACE_API_URL set), the console invites through the workspace with the
  person's own Tares Cloud session, the same call Settings > Workspace makes (TR-375, TR-1012):
  the control plane checks they own the workspace, adds the member, sends the invite email and
  tells the cell about the new person. The cell's POST answers 409 there.

Every string is worded for people. Names, emails, project names and repos are the person's own
text and the page shows them as text.
"""
from __future__ import annotations

import os

from fastapi import Body, HTTPException, Request

from .. import team as team_mod
from ..envelope import now_utc
from ..factory_home import plain_ago


def _cloud() -> bool:
    """A Tares Cloud cell: its people are the workspace's members, invited there."""
    return bool((os.getenv("TARES_WORKSPACE_API_URL") or "").strip())


def _workspace_url() -> str:
    return (os.getenv("TARES_WORKSPACE_URL") or "").strip()


def register(app, store, h, ctx):
    def admin(request: Request) -> bool:
        scopes = getattr(request.state, "scopes", None)
        return scopes is None or "admin" in scopes

    def rows(request: Request, now) -> list[dict]:
        hidden = h.hidden_ids()
        leads: dict[str, list[dict]] = {}
        for p in store.list_projects():
            if p.get("kind") != "software_factory" or p["id"] in hidden:
                continue
            lead = p.get("lead_person_id")
            if lead:
                leads.setdefault(lead, []).append({"id": p["id"], "name": p["name"],
                                                   "done": p.get("status") == "done"})
        owns: dict[str, list[str]] = {}
        try:
            from .. import areas as areas_mod
            for a in areas_mod.view(store):
                owns.setdefault(a["person"]["id"], []).append(a["repo"])
        except Exception:
            pass
        seen: dict[str, object] = {}
        for k in store.list_api_keys():
            pid, used = k.get("person_id"), k.get("last_used_at")
            if pid and used and (pid not in seen or used > seen[pid]):
                seen[pid] = used
        me = (h.current_person(request) or {}).get("id")
        out = []
        for p in store.list_people():
            used = seen.get(p["id"])
            first = p["id"] == "local" and not p.get("email")
            if first:     # the cell's first person, before anyone signed in (its stored name is "You")
                state, words = "active", "Uses this cell without signing in"
            elif used:
                state, words = "active", f"Last active {plain_ago(used, now)}"
            else:
                state, words = "invited", "Invited, not signed in yet"
            led = sorted(leads.get(p["id"], []), key=lambda x: (x["done"], x["name"].lower()))
            out.append({"id": p["id"], "name": "Local" if first else (p["name"] or p.get("email") or "Someone"),
                        "email": p.get("email"), "admin": bool(p.get("is_admin")),
                        "picture": p.get("picture"),
                        "you": p["id"] == me, "state": state, "state_words": words,
                        "leads": [{"id": x["id"], "name": x["name"]} for x in led if not x["done"]],
                        "owns": sorted(owns.get(p["id"], []))})
        out.sort(key=lambda r: (not r["you"], r["state"] != "active", r["name"].lower()))
        return out

    @app.get("/api/factory/people")
    def factory_people(request: Request):
        """{people: [{id, name, email, admin, picture, you, state: active|invited, state_words, leads:
        [{id, name}], owns: [repo]}], can_add, add_text, cloud, workspace_href, now}. You first,
        then the people who have signed in, then the invited ones."""
        now = now_utc()
        cloud = _cloud()
        can_add = cloud or admin(request)    # on Cloud the workspace decides (only its owner)
        if not can_add:
            text = "Only an admin can add people to this cell."
        elif cloud:
            text = ("Add a teammate by email. Tares Cloud sends them an invite, and they join this "
                    "workspace when they sign in.")
        else:
            text = ("Add a teammate by email. They sign in to this cell the way you do, and their "
                    "keys and crew are their own.")
        if can_add and not cloud:    # on Cloud the workspace decides, not the cell's licence
            ok, why = team_mod.get().can_add_person(store)
            if not ok:     # the licence has no seat for one more (or no licence): say why
                can_add, text = False, why
        return {"people": rows(request, now), "can_add": can_add, "add_text": text,
                "cloud": cloud, "workspace_href": _workspace_url() or None, "now": now.isoformat()}

    @app.post("/api/factory/people", status_code=201)
    def add_person(request: Request, body: dict = Body(...)):
        """{email, name?}: add a teammate on a self-hosted cell. Admin only (the default for a
        write). 201 with the person; 409 when that email is already on the cell, and on Tares
        Cloud, where the console invites through the workspace instead."""
        if _cloud():
            raise HTTPException(409, "On Tares Cloud, people join through the workspace: invite "
                                     "them from the People page while signed in to Tares Cloud")
        try:
            email = store.check_email((body or {}).get("email"))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not email:
            raise HTTPException(400, "enter an email address")
        name = " ".join(str((body or {}).get("name") or "").split())[:120] or None
        if store.find_person_by_email(email):
            raise HTTPException(409, f"{email} is already on this cell")
        ok, why = team_mod.get().can_add_person(store)
        if not ok:
            h.licence_required(why)
        p = store.person_or_create(email, name, via="admin")
        return {"id": p["id"], "name": p["name"] or email, "email": p.get("email")}
