"""A project's row and its lifecycle: create, read, change, pause, resume and delete (TR-1812).

A project is what a team works on: a name, a goal, an icon, a status, a lead and its members. What it
is made of lives beside it in the session store: its docs, skills, tickets, milestones and sessions.
There are no templates: a project's `template` (the stored `recipe` column) is `custom` for every
project made here and `default` for the cell's default project, and an old project keeps whatever it
was made from as data only. The routes accept a `template` field and ignore it.

The default project (store.default_project_id) is made the first time something has no other project
to sit in, so a cell where everything starts in a project has none. It takes a goal and an icon but
no new name, and cannot be deleted.
"""
from __future__ import annotations

import uuid

GOAL_MAX = 200
CUSTOM = "custom"
DEFAULT = "default"


class ProjectError(ValueError):
    """A change the caller asked for that cannot be made; the routes answer it with 400."""


def normalize_goal(goal) -> str | None:
    """A goal is one line: whitespace (new lines included) folds to single spaces and the ends
    are trimmed. Empty is None. Raises ValueError past GOAL_MAX characters."""
    if goal is None:
        return None
    text = " ".join(str(goal).split())
    if not text:
        return None
    if len(text) > GOAL_MAX:
        raise ValueError(f"a goal is at most {GOAL_MAX} characters (this one has {len(text)})")
    return text


def _icon(icon) -> str | None:
    from .project_icon import normalize_icon
    try:
        return normalize_icon(icon)
    except ValueError as e:
        raise ProjectError(str(e)) from e


def _goal(goal) -> str | None:
    try:
        return normalize_goal(goal)
    except ValueError as e:
        raise ProjectError(str(e)) from e


class Projects:
    """The cell's projects, over the session store. One per daemon (`Deps.projects`)."""

    def __init__(self, store):
        self.store = store

    # ── read ──
    def default_id(self) -> str | None:
        """The default project's id, or None on a cell that has none yet: reading never makes it."""
        return self.store.default_project_id_if_any()

    def get(self, uid: str) -> dict | None:
        row = self.store.get_project(uid)
        return self._card(row, self.default_id()) if row else None

    def list(self) -> list[dict]:
        """Every project, the default one first, then oldest first."""
        default = self.default_id()
        rows = sorted(self.store.list_projects(), key=lambda p: p["id"] != default)
        return [self._card(p, default) for p in rows]

    def summary(self, uid: str) -> dict:
        """The project with its log (created, updated, paused, resumed), newest first."""
        return {**self._require_card(uid), "log": self.store.list_project_log(uid)}

    def _card(self, row: dict, default: str | None) -> dict:
        """A project as the API returns it: its row, who leads it and who is on it, whether it is
        the default project, and `objects`, the skills it uses (each {kind, key, name, ...})."""
        uid = row["id"]
        objects = [{**o, "missing": False} for o in self.store.list_project_objects(uid)
                   if o["kind"] == "skill"]
        lead, members = self._who(row)
        return {**row, "objects": objects, "lead": lead, "members": members,
                "default": uid == default}

    def _who(self, row: dict) -> tuple[dict | None, list[dict]]:
        """The lead ({id, name}, None when it has none) and the members: the lead first with
        reason "lead", then the stored ones ("manual", "ticket" or "self")."""
        people = {p["id"]: p for p in self.store.list_people()}

        def ref(pid):
            return {"id": pid, "name": (people.get(pid) or {}).get("name") or pid}
        lead_id = row.get("lead_person_id")
        lead = ref(lead_id) if lead_id else None
        members = [{**lead, "reason": "lead"}] if lead else []
        members += [{**ref(m["person_id"]), "reason": m["reason"]}
                    for m in self.store.list_project_members(row["id"])
                    if m["person_id"] != lead_id]
        return lead, members

    # ── write ──
    def create(self, name: str, goal: str | None = None, icon: str | None = None,
               kind: str | None = None, lead_person_id: str | None = None) -> dict:
        """A new active project. `name` is its handle, unique on the cell; `goal` one line; `icon`
        one emoji or none; `kind` "software_factory" for a project the factory works in."""
        if kind not in (None, "", "software_factory"):
            raise ProjectError(f"unknown project kind {kind!r}; the one there is: software_factory")
        goal, icon = _goal(goal), _icon(icon)
        name = (name or "").strip()
        if not name:
            raise ProjectError("a project needs a name")
        if self.store.get_project_by_name(name) is not None:
            raise ProjectError(f"a project named {name!r} already exists")
        uid = "uc_" + uuid.uuid4().hex[:10]
        self.store.create_project(uid, CUSTOM, name, {}, status="active", goal=goal, icon=icon,
                                  lead_person_id=lead_person_id)
        if kind:
            self.store.set_project_kind(uid, kind)
        self.store.log_project(uid, "created", "")
        return self.get(uid)

    _UNSET = object()

    def update(self, uid: str, name: str | None = None, goal=_UNSET, icon=_UNSET) -> dict:
        """Change what is given: a new `name` (None or blank keeps it), the `goal` and the `icon`
        (left out keeps them, None or "" clears them). Everything is checked before anything is
        written, so a wrong value changes nothing. The default project takes a goal and an icon but
        no new name."""
        row = self._require(uid)
        name = (name or "").strip() or None
        if name and name != row["name"]:
            if self._is_default(row):
                raise ProjectError("the default project keeps its name")
            other = self.store.get_project_by_name(name)
            if other is not None and other["id"] != uid:
                raise ProjectError(f"a project named {name!r} already exists")
        new_goal = _goal(goal) if goal is not self._UNSET else self._UNSET
        new_icon = _icon(icon) if icon is not self._UNSET else self._UNSET
        changed = []
        if name and name != row["name"]:
            self.store.update_project(uid, name=name)
            changed.append(f"renamed to {name}")
        if new_goal is not self._UNSET:
            self.store.update_project(uid, goal=new_goal)
            changed.append("goal")
        if new_icon is not self._UNSET:
            self.store.set_project_icon(uid, new_icon)
            changed.append("icon")
        if changed:
            self.store.log_project(uid, "updated", ", ".join(changed))   # the summary's history shows it
        return self.get(uid)

    def pause(self, uid: str) -> dict:
        self._require(uid)
        self.store.update_project(uid, status="paused")
        self.store.log_project(uid, "paused", "")
        return self.get(uid)

    def resume(self, uid: str) -> dict:
        self._require(uid)
        self.store.update_project(uid, status="active", last_error=None)
        self.store.log_project(uid, "resumed", "")
        return self.get(uid)

    def delete(self, uid: str) -> dict:
        """Remove the project and everything that is only its (store.delete_project). The default
        project cannot be deleted."""
        row = self._require(uid)
        if self._is_default(row) or uid == self.default_id():
            raise ProjectError("the default project cannot be deleted")
        self.store.delete_project(uid)
        return {"ok": True}

    # ── internals ──
    @staticmethod
    def _is_default(row: dict) -> bool:
        return row.get("template") == DEFAULT

    def _require(self, uid: str) -> dict:
        row = self.store.get_project(uid)
        if row is None:
            raise KeyError(f"unknown project {uid!r}")
        return row

    def _require_card(self, uid: str) -> dict:
        self._require(uid)
        return self.get(uid)
