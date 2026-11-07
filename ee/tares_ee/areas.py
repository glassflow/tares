# Tares enterprise licence, see ee/LICENSE
"""Area owners (TR-627): which person owns each GitHub repo on the cell. An area is one repo,
`owner/name`, matched in lowercase. The list lives in the cell setting `areas` as JSON,
[{repo, person_id, note}], one entry per repo.

The `/api/areas` routes register from `register_routes`; the MCP tools are in `mcp_areas.py`.
`norm_repo` stays in core (`tares.factory`), because grants and sign-offs match repos the same way.
"""
from __future__ import annotations

import json
import threading

from fastapi import Body, HTTPException, Request

from tares import licence
from tares.factory import norm_repo

__all__ = ["AREAS_SETTING", "norm_repo", "load", "save", "owner", "view", "register_routes"]

AREAS_SETTING = "areas"
NEEDS_LICENCE = "Repo owners need an enterprise licence."


def load(store) -> list[dict]:
    """The saved entries, [{repo, person_id, note}]; empty when none or the setting is unreadable."""
    try:
        raw = json.loads(store.get_setting(AREAS_SETTING) or "[]")
    except ValueError:
        return []
    return [{"repo": e["repo"], "person_id": e["person_id"], "note": e.get("note") or ""}
            for e in raw if isinstance(e, dict) and e.get("repo") and e.get("person_id")]


def save(store, entries: list[dict]) -> list[dict]:
    """Validate and replace the whole list. The people must exist (ValueError says who); a repo
    given twice keeps its last entry. Returns what was saved."""
    by_repo: dict[str, dict] = {}
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError("each area is {repo, person_id, note?}")
        repo = norm_repo(e.get("repo"))
        pid = str(e.get("person_id") or "").strip()
        if not pid or store.get_person(pid) is None:
            raise ValueError(f"no person {pid!r}; GET /api/people lists them")
        note = e.get("note")
        if note is not None and not isinstance(note, str):
            raise ValueError("note is text")
        by_repo.pop(repo, None)
        by_repo[repo] = {"repo": repo, "person_id": pid, "note": (note or "").strip()}
    out = list(by_repo.values())
    store.set_setting(AREAS_SETTING, json.dumps(out))
    return out


def owner(store, repo) -> dict | None:
    """The person (a store person dict) who owns this repo, or None: no area for it, a repo
    that is not `owner/name`, or an owner who is no longer on the cell. Ignores case."""
    try:
        want = norm_repo(repo)
    except ValueError:
        return None
    for e in load(store):
        if e["repo"] == want:
            return store.get_person(e["person_id"])
    return None


def view(store) -> list[dict]:
    """What GET /api/areas returns: [{repo, person: {id, name}, note}], skipping an area whose
    person is gone."""
    out = []
    for e in load(store):
        p = store.get_person(e["person_id"])
        if p:
            out.append({"repo": e["repo"], "person": {"id": p["id"], "name": p["name"]},
                        "note": e["note"]})
    return out


owner_of = owner   # the route handlers below have a path parameter called `owner`


def register_routes(app, host) -> None:
    """The `/api/areas` routes. Each one answers `licence_required` unless the licence allows ee,
    before it does anything with the request (a body FastAPI cannot parse is a 422 first). Without
    ee at all the same paths answer from `tares.team.FALLBACKS`."""
    store = host.store
    lock = threading.Lock()

    def need_licence():
        if not licence.ee_allowed():
            host.licence_required(NEEDS_LICENCE)

    @app.get("/api/areas")
    def list_areas():
        """[{repo, person: {id, name}, note}], one per repo."""
        need_licence()
        return view(store)

    @app.put("/api/areas")
    def replace_areas(body: list = Body(...)):
        """Admin only. Replace the whole list with [{repo, person_id, note?}]. A repo given twice
        keeps its last entry; repos are saved in lowercase."""
        need_licence()
        try:
            with lock:
                save(store, body)
        except ValueError as e:
            host.err(e, 400)
        return view(store)

    @app.put("/api/areas/{owner}/{name}")
    def set_area(owner: str, name: str, request: Request, body: dict = Body(...)):
        """{person_id, note?}. An admin sets or reassigns any repo. Anyone else may only claim a
        repo nobody owns yet, for themselves: who they are comes from their key, never the body."""
        need_licence()
        try:
            repo = norm_repo(f"{owner}/{name}")
        except ValueError as e:
            host.err(e, 400)
        pid = str(body.get("person_id") or "").strip()
        if not pid or store.get_person(pid) is None:
            host.err(KeyError(f"no person {pid!r}; GET /api/people lists them"), 400)
        admin = host.is_admin(request)
        me = None
        if not admin:
            me = host.current_person(request)
            if not me:
                raise HTTPException(status_code=403, detail="this key belongs to no person, so it "
                                    "cannot claim a repo; ask an admin to set the owner")
            if pid != me["id"]:
                raise HTTPException(status_code=403, detail="you can claim a repo only for "
                                    "yourself; ask an admin to set another person as owner")
        # the unowned check, the load and the save are one step: handlers run in the thread
        # pool, and two claims at once would each save a list without the other's repo
        with lock:
            if not admin:
                held = owner_of(store, repo)
                if held and held["id"] != me["id"]:
                    raise HTTPException(status_code=403, detail=f"{repo} already belongs to "
                                        f"{held['name']}; ask an admin to reassign it")
            current = load(store)
            old = next((e for e in current if e["repo"] == repo), None)
            entries = [e for e in current if e["repo"] != repo]
            # a reassign keeps the note the repo had unless the body names one
            note = body["note"] if "note" in body else (old or {}).get("note")
            entries.append({"repo": repo, "person_id": pid, "note": note})
            try:
                save(store, entries)
            except ValueError as e:
                host.err(e, 400)
            # the answer is built before the lock is let go: a delete in between would leave
            # nothing to find
            out = next(a for a in view(store) if a["repo"] == repo)
        return out

    @app.delete("/api/areas/{owner}/{name}")
    def delete_area(owner: str, name: str):
        """Admin only. Take a repo's owner away."""
        need_licence()
        try:
            repo = norm_repo(f"{owner}/{name}")
        except ValueError as e:
            host.err(e, 400)
        with lock:
            current = load(store)
            left = [e for e in current if e["repo"] != repo]
            gone = len(left) != len(current)
            store.set_setting(AREAS_SETTING, json.dumps(left))
        return {"ok": True, "removed": gone}
