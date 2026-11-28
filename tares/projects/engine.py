"""The engine keeps a project's row, its goal, icon and status, and the skills it holds.

Tares 2.0 has no source catalog (TR-1811): a project holds no sources, triggers, agents or MCP
servers, so nothing is planned, adopted or wired here any more. Every project is a `custom` one (an
old template project reads as custom, projects/registry.py RETIRED) or the cell's `default` project.
What a project is made of now lives beside it: its docs, skills, tickets, milestones and sessions.

The default project (store.default_project_id) is made the first time something has no other
project to sit in, so a cell where everything starts in a project has none.
"""
from __future__ import annotations

import uuid

from ..goal import normalize_goal
from .base import ProjectError
from .icon import normalize_icon
from .registry import get_template, list_templates as _list_templates


class Engine:
    def __init__(self, store):
        self.store = store

    # ── read side ────────────────────────────────────────────────────────────
    def list_templates(self) -> list[dict]:
        return [r.describe() for r in _list_templates()]

    def default_id(self) -> str | None:
        """The default project's id, or None on a cell that has none yet: reading never makes
        it (it is made the first time something has no other project, store.default_project_id)."""
        return self.store.default_project_id_if_any()

    def get(self, uid: str) -> dict | None:
        inst = self.store.get_project(uid)
        if inst is None:
            return None
        return self._card(inst, self.default_id())

    def _card(self, inst: dict, default: str | None) -> dict:
        """A project as get() returns it: its row, its template's title, who leads it and who is
        on it. `objects` is the project's skills (the only part a project still lists)."""
        uid = inst["id"]
        objects = [{**o, "missing": False} for o in self.store.list_project_objects(uid)
                   if o["kind"] == "skill"]
        template = _safe_template(inst["template"])
        # `recipe` mirrors `template` for pre-1.14 clients
        title = template.title if template else inst["template"]
        lead, members = self._who(inst)
        return {**inst, "template_title": title, "objects": objects,
                "lead": lead, "members": members, "default": uid == default,
                "setup": None, "recipe": inst["template"], "recipe_title": title}

    def _who(self, inst: dict) -> tuple[dict | None, list[dict]]:
        """A project's lead ({id, name}, None when it has none) and its members: the lead first
        with reason "lead", then the stored ones ("manual", "ticket" or "self"). This is what the
        project says; who may do what is asked of the store."""
        people = {p["id"]: p for p in self.store.list_people()}

        def ref(pid):
            return {"id": pid, "name": (people.get(pid) or {}).get("name") or pid}
        lead_id = inst.get("lead_person_id")
        lead = ref(lead_id) if lead_id else None
        members = [{**lead, "reason": "lead"}] if lead else []
        members += [{**ref(m["person_id"]), "reason": m["reason"]}
                    for m in self.store.list_project_members(inst["id"])
                    if m["person_id"] != lead_id]
        return lead, members

    def list(self) -> list[dict]:
        default = self.default_id()
        rows = sorted(self.store.list_projects(), key=lambda u: u["id"] != default)
        return [self._card(u, default) for u in rows]

    def summary(self, uid: str) -> dict:
        inst = self.get(uid)
        if inst is None:
            raise KeyError(f"unknown project {uid!r}")
        return {**inst, "log": self.store.list_project_log(uid)}

    # ── write side ───────────────────────────────────────────────────────────
    def create(self, template_key: str, params: dict, name: str | None = None,
               goal: str | None = None, gone=None, icon: str | None = None) -> dict:
        """`goal`: what the project is for, one line. `icon`: one emoji shown beside the name;
        none = no icon. `gone` is accepted for the callers' sake: there is no object to hide."""
        if template_key == "default":
            raise ProjectError("every cell has one default project; Tares creates it")
        template = get_template(template_key)
        try:
            goal = normalize_goal(goal)
            icon = normalize_icon(icon)
        except ValueError as e:
            raise ProjectError(str(e)) from e
        params = template.validate(params)
        name = (name or "").strip()
        if not name:
            raise ProjectError("custom: a project needs a name")
        if self.store.get_project_by_name(name) is not None:
            raise ProjectError(f"a project named {name!r} already exists")
        uid = "uc_" + uuid.uuid4().hex[:10]
        self.store.create_project(uid, template.key, name, params, status="active", goal=goal,
                                  icon=icon)
        self.store.log_project(uid, "created", "")
        return self.get(uid)

    def update(self, uid: str, params: dict, gone=None) -> dict:
        """A project's settings: a custom project's only one is its (empty) object list."""
        inst = self._require(uid)
        if _is_default(inst["template"]):
            raise ProjectError("the default project has no settings to edit")
        template = get_template(inst["template"])
        params = template.validate(params)
        keep = {k: v for k, v in (inst.get("params") or {}).items() if k != "objects"}
        self.store.update_project(uid, params={**keep, **params}, last_error=None,
                                  status="active" if inst["status"] == "error" else None)
        self.store.log_project(uid, "updated", "no changes")
        report = {"created": [], "updated": [], "kept": [], "deleted": [], "added": [],
                  "reclaimed": [], "released": []}
        return {**self.get(uid), "report": report}

    def set_goal(self, uid: str, goal: str | None) -> dict:
        """Set or clear what the project is for. Any project, the default one included: the goal
        is the user's words, not configuration."""
        self._require(uid)
        try:
            goal = normalize_goal(goal)
        except ValueError as e:
            raise ProjectError(str(e)) from e
        self.store.update_project(uid, goal=goal)
        return self.get(uid)

    def set_icon(self, uid: str, icon: str | None) -> dict:
        """Set or clear the project's icon (one emoji, or None or ""). Any project, like the goal:
        it is how the person tells projects apart, not configuration. A wrong value is refused
        before anything changes."""
        self._require(uid)
        try:
            icon = normalize_icon(icon)
        except ValueError as e:
            raise ProjectError(str(e)) from e
        self.store.set_project_icon(uid, icon)
        return self.get(uid)

    def pause(self, uid: str, sources: bool = False) -> dict:
        """The project is marked paused. `sources` is accepted for old clients: there are none."""
        inst = self._require(uid)
        drop = ("resume_agents", "resume_triggers", "resume_wakes", "paused_sources")
        params = {k: v for k, v in (inst.get("params") or {}).items() if k not in drop}
        self.store.update_project(uid, params=params, status="paused")
        self.store.log_project(uid, "paused", "")
        return self.get(uid)

    def resume(self, uid: str) -> dict:
        inst = self._require(uid)
        drop = ("resume_agents", "resume_triggers", "resume_wakes", "paused_sources")
        self.store.update_project(uid, params={k: v for k, v in (inst.get("params") or {}).items()
                                               if k not in drop}, status="active")
        self.store.log_project(uid, "resumed")
        return self.get(uid)

    def delete(self, uid: str, purge_events: bool = False,
               delete_sources: list[str] | None = None) -> dict:
        """Remove the project. `purge_events` and `delete_sources` are accepted for old clients;
        a project holds no sources, so a source named answers as one that is not the project's."""
        inst = self._require(uid)
        if _is_default(inst["template"]) or uid == self.default_id():
            raise ProjectError("the default project cannot be deleted")
        unknown = [n for n in delete_sources or []]
        if unknown:
            raise ProjectError("not this project's sources: " + ", ".join(unknown))
        self.store.delete_project(uid)
        return {"ok": True, "deleted": [], "kept": [], "kept_shared": [], "released": [],
                "purged_events": 0, "purged_firings": 0}

    # ── internals ────────────────────────────────────────────────────────────
    def _require(self, uid: str) -> dict:
        inst = self.store.get_project(uid)
        if inst is None:
            raise KeyError(f"unknown project {uid!r}")
        return inst


def _is_default(template_key: str) -> bool:
    return template_key == "default"


def _safe_template(key: str):
    try:
        return get_template(key)
    except ProjectError:
        return None

