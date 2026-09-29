"""The engine turns a template's plan into real objects and keeps them in step with the params.

Every write goes through the catalog importer (`config.import_catalog_dict`), so a project's
objects get exactly the validation, secret handling and runtime start a catalog import gets. What
the engine adds is membership (see store.put_in_project: a trigger, agent or MCP server is in
exactly one project, a source in any number), a diff on update, all-or-nothing create,
pause/resume, delete and repair.

Every cell has a default project (store.default_project_id) that holds whatever no other project
does: objects created without naming a project, and objects a project lets go of.
"""
from __future__ import annotations

import traceback
import uuid

from .. import skills as _skills
from ..config import CatalogError, agent_url, import_catalog_dict
from ..goal import normalize_goal
from .base import PlannedObject, ProjectError
from .registry import get_template, list_templates as _list_templates

# delete order: dependents first (an agent references a trigger and may reference an mcp server,
# a trigger reads sources). A skill (TR-332) is the project's own and depends on nothing.
_DELETE_ORDER = ("agent", "trigger", "source", "mcp_server", "skill")
_SECTION = {"source": "sources", "trigger": "triggers",
            "agent": "agents", "mcp_server": "mcp_servers"}
# the kinds that belong to exactly one project (a source is shared)
_SOLE = ("trigger", "agent", "mcp_server")


class Engine:
    def __init__(self, store, reload=None, runtime=None):
        """`reload`: called after every catalog change (the runtime's reload_catalog); None while
        the daemon is still booting, since the runtime builds its catalog from the DB afterwards.
        `runtime`: handed to a template's after_create hook (bootstrap runs); None at boot."""
        self.store = store
        self._reload = reload
        self.runtime = runtime

    # ── read side ────────────────────────────────────────────────────────────
    def list_templates(self) -> list[dict]:
        return [r.describe() for r in _list_templates()]

    def _existing_names(self, uid: str | None = None) -> dict[str, dict[str, dict]]:
        """The catalog by kind and name; with `uid`, also that project's skills (a skill name is
        only unique within its project)."""
        s = self.store
        return {"source": {x["name"]: x for x in s.list_catalog_sources()},
                "trigger": {x["name"]: x for x in s.list_catalog_triggers()},
                "agent": {x["name"]: x for x in s.list_catalog_agents()},
                "mcp_server": {x["name"]: x for x in s.list_mcp_servers()},
                "skill": ({x["name"]: {**x, "owned_by": uid} for x in s.list_skills(uid)}
                          if uid else {})}

    def default_id(self) -> str:
        return self.store.default_project_id()

    def get(self, uid: str) -> dict | None:
        inst = self.store.get_project(uid)
        if inst is None:
            return None
        existing = self._existing_names(uid)
        objects = []
        for o in self.store.list_project_objects(uid):
            if o["kind"] not in existing:   # a kind that no longer exists (a folded view)
                continue
            row = existing[o["kind"]].get(o["name"])
            # an object deleted by hand and recreated under another project, or moved to one, is
            # lost to this one: missing, and nothing here acts on it any more. A source is shared,
            # so its creator says nothing about whether it is still here.
            lost = (o["kind"] != "source" and row is not None
                    and row.get("owned_by") not in (None, uid))
            objects.append({**o, "missing": row is None or lost,
                            "customized": bool(row and row.get("customized")) or o["customized"]})
        template = _safe_template(inst["template"])
        # `recipe` mirrors `template` for pre-1.14 clients; dropped two releases after 1.14
        title = template.title if template else inst["template"]
        return {**inst, "template_title": title, "objects": objects,
                "default": uid == self.default_id(),
                "recipe": inst["template"], "recipe_title": title}

    def list(self) -> list[dict]:
        default = self.default_id()
        rows = sorted(self.store.list_projects(), key=lambda u: u["id"] != default)
        return [self.get(u["id"]) for u in rows]

    def summary(self, uid: str) -> dict:
        inst = self.get(uid)
        if inst is None:
            raise KeyError(f"unknown project {uid!r}")
        template = _safe_template(inst["template"])
        extra = {}
        if template is not None:
            try:
                extra = template.summary(inst, self.store) or {}
            except Exception as e:   # a summary must never take the page down
                extra = {"summary_error": f"{type(e).__name__}: {e}"}
        # the instance's own fields win: a template summary adds detail, it never replaces
        # id/objects/status/log that the pages depend on
        base = {**inst, "log": self.store.list_project_log(uid)}
        return {**{k: v for k, v in extra.items() if k not in base}, **base}

    # ── write side ───────────────────────────────────────────────────────────
    def create(self, template_key: str, params: dict, name: str | None = None,
               goal: str | None = None) -> dict:
        """`goal`: what the project is for, one line; none given = the template's GOAL."""
        if template_key == "default":
            raise ProjectError("every cell has one default project; Tares creates it")
        template = get_template(template_key)
        try:
            goal = normalize_goal(goal) or normalize_goal(getattr(template, "GOAL", "") or None)
        except ValueError as e:
            raise ProjectError(str(e)) from e
        params = template.validate(params)
        template.preflight(params, self.store)
        name = (name or "").strip()
        if not name and _is_custom(template_key):
            # a template title is a sensible default name; "From existing objects" is not
            raise ProjectError("custom: a hand-assembled project needs a name")
        name = name or f"{template.title or template.key}"
        if self.store.get_project_by_name(name) is not None:
            raise ProjectError(f"a project named {name!r} already exists")
        plan = template.plan(params)
        uid = "uc_" + uuid.uuid4().hex[:10]
        self.store.create_project(uid, template.key, name, params, status="active", goal=goal)
        self.store.log_project(uid, "create", f"{len(plan)} objects planned")
        before = self._existing_names(uid)
        try:
            # a custom project shares sources rather than taking them
            self._check_ownership(uid, [o for o in plan if not (_is_custom(template.key)
                                                             and o.kind == "source")], before)
            if _is_custom(template.key):
                self._adopt(uid, plan, before)
            else:
                self._apply(uid, plan)
        except Exception as e:
            # all or nothing: remove what this create added (never what already existed), record
            # the failure on the instance so the UI can show it, then re-raise.
            if _is_custom(template.key):
                # _adopt already undid its own writes (it never touches another project's
                # objects); with nothing created there is nothing to show on an error page, so
                # the row goes and the caller gets the reason
                self.store.delete_project(uid)
                self._do_reload()
                raise
            created = [o for o in plan if o.name not in before[o.kind]]
            self._delete_objects(created, purge_events=False, uid=uid)
            self.store.update_project(uid, status="error", last_error=_errtext(e))
            self.store.log_project(uid, "create_failed", _errtext(e))
            self._do_reload()
            raise
        self._do_reload()
        self.store.log_project(uid, "created", ", ".join(f"{o.kind}:{o.name}" for o in plan))
        inst = self.get(uid)
        try:
            template.after_create(inst, self.store, self.runtime)
        except Exception as e:   # the objects exist; a failed bootstrap is a note, not a rollback
            self.store.log_project(uid, "bootstrap_failed", _errtext(e))
        return self.get(uid)

    def update(self, uid: str, params: dict) -> dict:
        inst = self._require(uid)
        if _is_default(inst["template"]):
            raise ProjectError("the default project has no settings to edit; move objects into "
                               "another project by setting their project")
        template = get_template(inst["template"])
        params = template.validate(params)
        template.preflight(params, self.store)
        plan = template.plan(params)
        if _is_custom(inst["template"]):
            return self._update_custom(uid, inst, params, plan)
        # rows keyed `+kind:name` were added to this project by hand, not planned: a re-plan
        # leaves them alone
        existing = {(o["kind"], o["key"]): o for o in self.store.list_project_objects(uid)
                    if not o["key"].startswith("+")}
        current = self._existing_names(uid)
        report = {"created": [], "updated": [], "kept": [], "deleted": []}
        to_apply: list[PlannedObject] = []
        for o in plan:
            prev = existing.get((o.kind, o.key))
            if prev is None:
                report["created"].append(f"{o.kind}:{o.name}")
                to_apply.append(o)
                continue
            row = current[o.kind].get(prev["name"])
            if row is not None and (row.get("customized") or prev["customized"]):
                report["kept"].append(f"{o.kind}:{prev['name']}")
                continue
            if row is not None and prev["name"] != o.name:
                # the plan renamed this object: drop the old name, the new one is created below
                self._retire(uid, [PlannedObject(o.kind, o.key, {"name": prev["name"]})])
            report["updated" if row is not None else "created"].append(f"{o.kind}:{o.name}")
            to_apply.append(o)
        planned_keys = {(o.kind, o.key) for o in plan}
        removed = [PlannedObject(k, key, {"name": o["name"]})
                   for (k, key), o in existing.items() if (k, key) not in planned_keys]
        self._check_ownership(uid, to_apply, current)
        self._retire(uid, removed)
        for r in removed:
            self.store.delete_project_object(uid, r.kind, r.key)
            report["deleted"].append(f"{r.kind}:{r.name}")
        try:
            self._apply(uid, to_apply)
        except Exception as e:
            self.store.update_project(uid, status="error", last_error=_errtext(e))
            self.store.log_project(uid, "update_failed", _errtext(e))
            self._do_reload()
            raise
        self.store.update_project(uid, params=params, last_error=None,
                                  status="active" if inst["status"] == "error" else None)
        self._do_reload()
        self.store.log_project(uid, "updated", "; ".join(
            f"{k}: {', '.join(v)}" for k, v in report.items() if v) or "no changes")
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

    def fill_template_goals(self) -> int:
        """Upgrade, once per database: a project made from a template before goals existed gets
        its template's GOAL. A custom or default project stays without one (the console asks).
        The settings marker keeps a goal the user cleared later from coming back on a restart.
        Returns how many projects were given a goal."""
        if self.store.get_setting("template_goals_filled"):
            return 0
        n = 0
        for p in self.store.list_projects():
            if p.get("goal"):
                continue
            template = _safe_template(p["template"])
            goal = normalize_goal(getattr(template, "GOAL", "") or None) if template else None
            if goal:
                self.store.update_project(p["id"], goal=goal)
                n += 1
        self.store.set_setting("template_goals_filled", "1")
        return n

    def pause(self, uid: str, sources: bool = False) -> dict:
        """Triggers off, agents unsubscribed; sources keep ingesting unless `sources` is set, in
        which case the project's sources that are running are paused too (polling stops, pushes
        are refused) and remembered, so resume brings back exactly those: a source paused by hand
        before stays paused."""
        inst = self._require(uid)
        objects = self._live_objects(uid)
        if _adopted(inst["template"]) and inst["status"] != "paused":
            # no planned version says which agents were on: remember it for resume (a repeated
            # pause finds nothing on and must not forget the first answer)
            on = [o["name"] for o in objects if o["kind"] == "agent"
                  and self.store.subscription_by_url(agent_url(o["name"]))]
            paused = {t["name"] for t in self.store.list_catalog_triggers() if t.get("paused")}
            live = [o["name"] for o in objects if o["kind"] == "trigger" and o["name"] not in paused]
            self.store.update_project(uid, params={**inst["params"], "resume_agents": on,
                                                   "resume_triggers": live})
        for o in objects:
            if o["kind"] == "trigger":
                self.store.set_trigger_paused(o["name"], True)
            elif o["kind"] == "agent":
                self.store.remove_subscription_by_url(agent_url(o["name"]))
        paused_sources: list[str] = []
        if sources:
            running = {s["name"] for s in self.store.list_catalog_sources() if not s["paused"]}
            paused_sources = [o["name"] for o in objects if o["kind"] == "source" and o["name"] in running]
            for name in paused_sources:
                self.store.set_source_paused(name, True)
            inst = self._require(uid)   # params may have just been rewritten above
            self.store.update_project(uid, params={**inst["params"], "paused_sources": paused_sources})
        self.store.update_project(uid, status="paused")
        n = len(paused_sources)
        self.store.log_project(uid, "paused", "triggers paused, agents unsubscribed; "
                               + (f"{n} source{'s' if n != 1 else ''} paused" if sources else "sources keep ingesting"))
        self._do_reload()
        return self.get(uid)

    def resume(self, uid: str) -> dict:
        inst = self._require(uid)
        template = get_template(inst["template"])
        plan = {(o.kind, o.key): o for o in template.plan(template.validate(inst["params"]))}
        adopted = _adopted(inst["template"])
        resume_agents = set(inst["params"].get("resume_agents") or []) if adopted else set()
        resume_triggers = set(inst["params"].get("resume_triggers") or []) if adopted else set()
        for o in self._live_objects(uid):
            if o["kind"] == "trigger":
                # a custom project's trigger that the user had paused before stays paused
                if not adopted or o["name"] in resume_triggers:
                    self.store.set_trigger_paused(o["name"], False)
            elif o["kind"] == "agent":
                spec = plan.get(("agent", o["key"]))
                agent = self.store.get_catalog_agent(o["name"])
                wanted = (o["name"] in resume_agents) if adopted else bool(
                    spec is not None and spec.spec.get("enabled", False))
                if agent and wanted:
                    url = agent_url(o["name"])
                    if not self.store.subscription_by_url(url):
                        self.store.add_subscription("sub_" + uuid.uuid4().hex[:8],
                                                    agent["trigger"], url, created_by="tares")
        # the sources this pause stopped come back; one paused by hand before is left alone
        paused_sources = list(inst["params"].get("paused_sources") or [])
        live_sources = {o["name"] for o in self._live_objects(uid) if o["kind"] == "source"}
        for name in paused_sources:
            if name in live_sources:
                self.store.set_source_paused(name, False)
        if adopted or paused_sources:
            drop = ("resume_agents", "resume_triggers") if adopted else ()
            self.store.update_project(uid, params={k: v for k, v in inst["params"].items()
                                                   if k not in drop and k != "paused_sources"})
        self.store.update_project(uid, status="active")
        if paused_sources:
            n = len(paused_sources)
            self.store.log_project(uid, "resumed", f"{n} source{'s' if n != 1 else ''} resumed")
        else:
            self.store.log_project(uid, "resumed")
        self._do_reload()
        return self.get(uid)

    def delete(self, uid: str, purge_events: bool = False,
               delete_sources: list[str] | None = None) -> dict:
        """Remove the project with its triggers, agents and MCP servers. Its sources stay, since
        other projects may read them, unless named in `delete_sources`: each of those is deleted
        too when no other project uses it, and kept (and reported) when one does. A source this
        project leaves behind in no project joins the default project. `purge_events` purges the
        events of the sources deleted and the firings of the triggers deleted."""
        inst = self._require(uid)
        if _is_default(inst["template"]) or uid == self.default_id():
            raise ProjectError("the default project cannot be deleted")
        rows = self.store.list_project_objects(uid)
        members = sorted({o["name"] for o in rows if o["kind"] == "source"})
        chosen = []
        for n in delete_sources or []:
            if n not in chosen:
                chosen.append(n)
        unknown = [n for n in chosen if n not in members]
        if unknown:
            raise ProjectError("not this project's sources: " + ", ".join(unknown))
        existing = self._existing_names()
        # only what is still this project's goes: an object moved elsewhere since is left alone
        going = [PlannedObject(o["kind"], o["key"], {"name": o["name"]}) for o in rows
                 if o["kind"] in _SOLE
                 and (existing[o["kind"]].get(o["name"]) or {}).get("owned_by") == uid]
        self._delete_objects(going, purge_events=False)
        # firings go with the events: a purged project leaves no history behind its triggers
        purged_firings = sum(self.store.purge_dispatches(o.name)
                             for o in going if o.kind == "trigger") if purge_events else 0
        self.store.delete_project(uid)
        # the sources it created and leaves behind have no creator any more
        for n in members:
            if (existing["source"].get(n) or {}).get("owned_by") == uid:
                self.store.set_owned_by("source", n, None)
        # with this project's rows gone, a source still listed anywhere is used by another project
        still = self.store.source_memberships()
        deleted_sources = [n for n in chosen if not still.get(n) and n in existing["source"]]
        kept = [n for n in chosen if n not in deleted_sources]
        purged = self._delete_objects([PlannedObject("source", f"source:{n}", {"name": n})
                                       for n in deleted_sources], purge_events=purge_events)
        self._do_reload()
        if self._reload is None:
            self.store.normalize_projects()   # no runtime yet: place the orphans here
        return {"ok": True,
                "deleted": [f"{o.kind}:{o.name}" for o in going]
                + [f"source:{n}" for n in deleted_sources],
                "kept": kept,
                "released": [f"source:{n}" for n in members if n not in deleted_sources],
                "purged_events": purged, "purged_firings": purged_firings}

    # ── sources: shared, added to and removed from a project by hand ────────
    def add_source(self, uid: str, name: str) -> dict:
        self._require(uid)
        if name not in {s["name"] for s in self.store.list_catalog_sources()}:
            raise KeyError(f"unknown source {name!r}")
        self.store.put_in_project("source", name, uid)
        self.store.log_project(uid, "source_added", name)
        self._do_reload()
        return self.get(uid)

    def remove_source(self, uid: str, name: str) -> dict:
        inst = self._require(uid)
        if name not in self.store.project_sources(uid):
            raise KeyError(f"source {name!r} is not in this project")
        users = [t["name"] for t in self.store.list_catalog_triggers()
                 if t.get("owned_by") == uid and name in (t.get("sources") or [])]
        if users:
            raise ProjectError(f"source {name!r} is read by this project's trigger"
                               f"{'s' if len(users) != 1 else ''} {', '.join(users)}; change "
                               f"{'them' if len(users) != 1 else 'it'} first")
        if _is_default(inst["template"]) and not [u for u in self.store.source_memberships()
                                                  .get(name, []) if u != uid]:
            raise ProjectError(f"source {name!r} is in no other project; add it to one first, "
                               "or delete the source")
        self.store.remove_from_project("source", name, uid)
        self.store.log_project(uid, "source_removed", name)
        self._do_reload()
        return self.get(uid)

    async def detect(self, template_key: str) -> dict:
        template = get_template(template_key)
        return await template.detect(self.store, self.runtime)

    def action(self, uid: str, name: str, args: dict | None = None) -> dict:
        """Run one of the template's ACTIONS on an instance and log it."""
        inst = self._require(uid)
        template = get_template(inst["template"])
        if not any(a.get("name") == name for a in template.ACTIONS):
            raise ProjectError(f"{template.key}: no action {name!r}")
        result = template.run_action(inst, name, dict(args or {}), self.store, self.runtime) or {}
        self.store.log_project(uid, f"action:{name}", str(result.get("message", "")) if result else "")
        return {"ok": True, "action": name, **result}

    def repair(self, uid: str, key: str) -> dict:
        """Re-apply one planned object from the current params: re-creates a hand-deleted object,
        or resets a customized one back to the plan (ownership is re-claimed either way)."""
        inst = self._require(uid)
        if _adopted(inst["template"]):
            raise ProjectError("a project assembled from existing objects has no planned version "
                               "to repair; edit the project to change its objects")
        template = get_template(inst["template"])
        plan = template.plan(template.validate(inst["params"]))
        target = next((o for o in plan if o.key == key or f"{o.kind}:{o.key}" == key), None)
        if target is None:
            raise ProjectError(f"no planned object with key {key!r}")
        prev = next((o for o in self.store.list_project_objects(uid)
                     if o["kind"] == target.kind and o["key"] == target.key), None)
        if prev and prev["name"] != target.name:
            self._retire(uid, [PlannedObject(target.kind, target.key, {"name": prev["name"]})])
        self._check_ownership(uid, [target], self._existing_names(uid))
        self._apply(uid, [target])
        self._do_reload()
        self.store.log_project(uid, "repaired", f"{target.kind}:{target.name}")
        return self.get(uid)

    # ── internals ────────────────────────────────────────────────────────────
    def _live_objects(self, uid: str) -> list[dict]:
        """The project's objects that are still its to act on (see get(): an object recreated or
        moved under another project is missing here)."""
        return [o for o in (self.get(uid) or {}).get("objects", []) if not o["missing"]]

    def _require(self, uid: str) -> dict:
        inst = self.store.get_project(uid)
        if inst is None:
            raise KeyError(f"unknown project {uid!r}")
        return inst

    def _check_ownership(self, uid: str, plan: list[PlannedObject], existing: dict) -> None:
        """A plan may take an object of the same name that sits in the default project (upsert),
        never one that belongs to another project. A source is shared, but a template reconfigures
        what it plans, so one created by another project is still not a template's to take."""
        default = self.default_id()
        alive = {p["id"] for p in self.store.list_projects()}
        for o in plan:
            row = existing[o.kind].get(o.name)
            # a project that no longer exists owns nothing (a source records its creator)
            if (row is not None and row.get("owned_by") not in (None, uid, default)
                    and row["owned_by"] in alive):
                raise ProjectError(f"{o.kind} {o.name!r} belongs to another project "
                                   f"({self._project_label(row['owned_by'])})")

    def _project_label(self, uid: str) -> str:
        p = self.store.get_project(uid)
        return f"{p['name']}, {uid}" if p else uid

    def _apply(self, uid: str, plan: list[PlannedObject]) -> None:
        if not plan:
            return
        # skills are checked with the rest of the plan, before anything is written, and stored
        # in the project directly: they are not catalog objects
        skills = []
        for o in plan:
            if o.kind == "skill":
                try:
                    skills.append((o, _skills.validate(o.name, o.spec.get("description"),
                                                       o.spec.get("body"))))
                except _skills.SkillError as e:
                    raise ProjectError(str(e)) from e
        plan = [o for o in plan if o.kind != "skill"]
        doc: dict = {}
        for o in plan:
            doc.setdefault(_SECTION[o.kind], []).append(dict(o.spec))
        # a source that was already there is used, not created: this project never becomes its
        # creator, so deleting the project cannot take it (the plugin's claude_code source)
        had = {s["name"] for s in self.store.list_catalog_sources()}
        try:
            # validates the whole doc, then writes; this engine places what it applies
            import_catalog_dict(self.store, doc, assign=False)
        except CatalogError as e:
            raise ProjectError(str(e)) from e
        for o in plan:
            if not (o.kind == "source" and o.name in had):
                self.store.set_owned_by(o.kind, o.name, uid)
            self.store.put_in_project(o.kind, o.name, uid, key=o.key)
        # a trigger's sources are members of its project, planned or not (the findings source a
        # handoff trigger reads, say)
        for o in plan:
            if o.kind == "trigger":
                for src in o.spec.get("sources") or []:
                    self.store.put_in_project("source", src, uid)
        for o, (name, description, body) in skills:
            self.store.upsert_skill(uid, name, description, body)
            self.store.upsert_project_object(uid, "skill", o.key, name)

    # ── custom projects: adopt and release instead of create and delete ─────
    def _adopt(self, uid: str, objs: list[PlannedObject], existing: dict) -> None:
        """Take objects that already exist into this project. Checks everything first (the object
        exists; a trigger, agent or MCP server is in the default project or already here), then
        writes; a failure part-way releases what this call took, so membership is never left
        half applied. What an object needs comes along from the default project: an agent's
        trigger and MCP servers, a trigger's agents and its sources."""
        for o in objs:
            if o.name not in existing[o.kind]:
                raise ProjectError(f"{o.kind} {o.name!r} does not exist")
        self._check_ownership(uid, [o for o in objs if o.kind != "source"], existing)
        done: list[PlannedObject] = []
        # sources last: a source leaves the default project only once no trigger there reads it,
        # which is known after the triggers have moved
        order = {"trigger": 0, "agent": 1, "mcp_server": 2, "source": 3}
        try:
            for o in sorted(objs, key=lambda o: order[o.kind]):
                self.store.put_in_project(o.kind, o.name, uid, key=f"{o.kind}:{o.name}")
                done.append(o)
            for extra in sorted(self._closure(uid, [(o.kind, o.name) for o in objs], existing),
                                key=lambda x: order[x[0]]):
                self.store.put_in_project(extra[0], extra[1], uid)
                done.append(PlannedObject(extra[0], f"{extra[0]}:{extra[1]}", {"name": extra[1]}))
        except Exception:
            self._release(uid, done)
            raise

    def _closure(self, uid: str, taken: list[tuple[str, str]], existing: dict) -> list:
        """What else must come into project `uid` with `taken`: an agent's trigger and MCP
        servers, a trigger's agents, a trigger's sources. Raises when one of them belongs to
        another project (not the default one), naming it."""
        default = self.default_id()
        have = set(taken)
        out: list[tuple[str, str]] = []
        queue = list(taken)

        def need(kind, name, why):
            if (kind, name) in have:
                return
            row = existing[kind].get(name)
            if row is None:
                return
            if kind != "source" and row.get("owned_by") not in (None, uid, default):
                raise ProjectError(f"{why} {kind} {name!r}, which belongs to another project "
                                   f"({self._project_label(row['owned_by'])}); add or move it "
                                   "first")
            if kind != "source" and row.get("owned_by") == uid:
                return
            have.add((kind, name))
            out.append((kind, name))
            queue.append((kind, name))

        while queue:
            kind, name = queue.pop(0)
            row = existing[kind].get(name) or {}
            if kind == "agent":
                need("trigger", row.get("trigger"), f"agent {name!r} runs on")
                for srv in row.get("mcp_servers") or []:
                    need("mcp_server", srv, f"agent {name!r} uses")
            elif kind == "trigger":
                for a in existing["agent"].values():
                    if a.get("trigger") == name:
                        need("agent", a["name"], f"trigger {name!r} wakes")
                for src in row.get("sources") or []:
                    need("source", src, f"trigger {name!r} reads")
        return out

    def _release_closure(self, uid: str, removed: list[PlannedObject], plan: list[PlannedObject],
                         current: dict) -> list[PlannedObject]:
        """What a custom project's edit lets go of, whole: a released trigger takes this
        project's agents on it along (an agent is always in its trigger's project). Refused,
        naming both, when what stays needs what goes: an agent whose trigger stays, an MCP server
        an agent that stays uses, a source a trigger that stays reads."""
        staying = {(o.kind, o.name) for o in plan}
        going = {(o.kind, o.name) for o in removed}
        out = list(removed)
        for o in removed:
            if o.kind == "trigger":
                for a in current["agent"].values():
                    if (a.get("trigger") == o.name and a.get("owned_by") == uid
                            and ("agent", a["name"]) not in going):
                        if ("agent", a["name"]) in staying:
                            raise ProjectError(f"agent {a['name']!r} stays but runs on trigger "
                                               f"{o.name!r}; release both or keep both")
                        going.add(("agent", a["name"]))
                        out.append(PlannedObject("agent", f"agent:{a['name']}",
                                                 {"name": a["name"]}))
        for kind, name in sorted(going):
            if kind == "agent":
                trig = (current["agent"].get(name) or {}).get("trigger")
                if ("trigger", trig) in staying:
                    raise ProjectError(f"agent {name!r} runs on trigger {trig!r}, which stays in "
                                       "this project; an agent is always in its trigger's project")
            elif kind == "mcp_server":
                users = [a["name"] for a in current["agent"].values()
                         if name in (a.get("mcp_servers") or []) and ("agent", a["name"]) in staying]
                if users:
                    raise ProjectError(f"MCP server {name!r} is used by agent"
                                       f"{'s' if len(users) != 1 else ''} {', '.join(users)}, "
                                       "which stay in this project")
            elif kind == "source":
                users = [t["name"] for t in current["trigger"].values()
                         if name in (t.get("sources") or []) and ("trigger", t["name"]) in staying]
                if users:
                    raise ProjectError(f"source {name!r} is read by trigger"
                                       f"{'s' if len(users) != 1 else ''} {', '.join(users)}, "
                                       "which stay in this project")
        return out

    def _release(self, uid: str, objs: list[PlannedObject]) -> None:
        """Let go of objects; they stay exactly as they are and move to the default project (a
        source stays in its other projects, and joins the default one only if it has none)."""
        for o in objs:
            self.store.remove_from_project(o.kind, o.name, uid)
            self.store.delete_project_object(uid, o.kind, o.key)

    def _retire(self, uid: str, objs: list[PlannedObject]) -> None:
        """A template's re-plan dropped these: delete them, except a source another project also
        uses, which only leaves this project."""
        memberships = self.store.source_memberships()
        gone = []
        for o in objs:
            if o.kind == "source" and [u for u in memberships.get(o.name, []) if u != uid]:
                self.store.remove_from_project("source", o.name, uid)
            else:
                gone.append(o)
        self._delete_objects(gone, purge_events=False, uid=uid)

    def _update_custom(self, uid: str, inst: dict, params: dict, plan: list[PlannedObject]) -> dict:
        existing = {(o["kind"], o["key"]): o for o in self.store.list_project_objects(uid)}
        planned = {(o.kind, o.key) for o in plan}
        added = [o for o in plan if (o.kind, o.key) not in existing]
        removed = [PlannedObject(k, key, {"name": o["name"]})
                   for (k, key), o in existing.items() if (k, key) not in planned]
        current = self._existing_names()
        # a kept object that was deleted by hand and recreated under the same name sits in the
        # default project now: re-adopt it
        default = self.default_id()
        reclaim = [o for o in plan if (o.kind, o.key) in existing and o.kind != "source"
                   and (row := current[o.kind].get(o.name)) is not None
                   and row.get("owned_by") in (None, default)]
        added = added + reclaim
        removed = self._release_closure(uid, removed, plan, current)
        # validate before the first write: the additions must exist and be free
        for o in added:
            if o.name not in current[o.kind]:
                raise ProjectError(f"{o.kind} {o.name!r} does not exist")
        self._check_ownership(uid, [o for o in added if o.kind != "source"], current)
        try:
            self._release(uid, removed)
            self._adopt(uid, added, self._existing_names())
        except Exception as e:
            self._adopt(uid, [o for o in removed if o.name in current[o.kind]],
                        self._existing_names())
            self.store.update_project(uid, status="error", last_error=_errtext(e))
            self.store.log_project(uid, "update_failed", _errtext(e))
            self._do_reload()
            raise
        resume_agents = list(inst["params"].get("resume_agents") or [])
        resume_triggers = list(inst["params"].get("resume_triggers") or [])
        if inst["status"] == "paused":
            # additions join a paused project paused: triggers off, agents unsubscribed and
            # remembered, so resume brings back exactly what was on. An agent that was already
            # off when added stays off on resume: the user disabled it on its own page, and a
            # project resume must not undo that (enable it there).
            paused_now = {t["name"] for t in self.store.list_catalog_triggers() if t.get("paused")}
            for o in added:
                if o.kind == "trigger":
                    if o.name not in paused_now:
                        resume_triggers.append(o.name)
                    self.store.set_trigger_paused(o.name, True)
                elif o.kind == "agent" and self.store.subscription_by_url(agent_url(o.name)):
                    self.store.remove_subscription_by_url(agent_url(o.name))
                    resume_agents.append(o.name)
            gone = {o.name for o in removed}
            resume_agents = [a for a in resume_agents if a not in gone]
            resume_triggers = [t for t in resume_triggers if t not in gone]
        keep = ({"resume_agents": resume_agents, "resume_triggers": resume_triggers}
                if inst["status"] == "paused" else {})
        # the adoption above may have pulled in what the objects need: the stored list is the
        # truth, the params only add the pause bookkeeping
        objects = (self.store.get_project(uid) or {}).get("params", {}).get("objects") \
            or params.get("objects") or []
        self.store.update_project(uid, params={**keep, **params, "objects": objects},
                                  last_error=None,
                                  status="active" if inst["status"] == "error" else None)
        self._do_reload()
        report = {"created": [], "updated": [], "deleted": [],
                  "added": [f"{o.kind}:{o.name}" for o in added if o not in reclaim],
                  "reclaimed": [f"{o.kind}:{o.name}" for o in reclaim],
                  "released": [f"{o.kind}:{o.name}" for o in removed],
                  "kept": [f"{o.kind}:{o.name}" for o in plan if (o.kind, o.key) in existing]}
        self.store.log_project(uid, "updated", "; ".join(
            f"{k}: {', '.join(v)}" for k, v in report.items() if v) or "no changes")
        return {**self.get(uid), "report": report}

    def _delete_objects(self, objs: list[PlannedObject], purge_events: bool,
                        uid: str | None = None) -> int:
        """`uid`: the project a planned skill is deleted from."""
        purged = 0
        s = self.store
        for kind in _DELETE_ORDER:
            for o in objs:
                if o.kind != kind:
                    continue
                if kind == "agent":
                    s.remove_subscription_by_url(agent_url(o.name))
                    s.delete_catalog_agent(o.name)
                elif kind == "trigger":
                    s.delete_catalog_trigger(o.name)
                    s.remove_subscriptions_by_trigger(o.name)
                elif kind == "source":
                    s.delete_catalog_source(o.name)
                    if purge_events:
                        purged += s.purge_events(o.name)
                elif kind == "mcp_server":
                    s.delete_mcp_server(o.name)
                elif kind == "skill" and uid:
                    s.delete_skill(uid, o.name)
        return purged

    def _do_reload(self) -> None:
        if self._reload is not None:
            self._reload()


def dependents(store, kind: str, name: str) -> list[dict]:
    """Everything that stops working if this object goes, in delete order: a source's triggers
    (those that read it) and their agents; a trigger's agents. Each entry is {kind, name}. Used by
    the delete dialogs to say what else goes, and by the cascade deletes to take it along."""
    triggers = store.list_catalog_triggers()
    agents = store.list_catalog_agents()
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(k, n):
        if (k, n) not in seen:
            seen.add((k, n))
            out.append({"kind": k, "name": n})

    trigger_names = [name] if kind == "trigger" else []
    if kind == "source":
        trigger_names = [t["name"] for t in triggers if name in (t.get("sources") or [])]
    agent_names = [a["name"] for a in agents if a.get("trigger") in trigger_names]
    for n in agent_names:
        add("agent", n)
    for n in trigger_names:
        if kind != "trigger" or n != name:
            add("trigger", n)
    return out


def _is_custom(template_key: str) -> bool:
    return template_key == "custom"


def _is_default(template_key: str) -> bool:
    return template_key == "default"


def _adopted(template_key: str) -> bool:
    """A project whose objects were placed in it rather than planned: nothing to re-plan, repair
    or reset, and a pause has to remember what was on."""
    return template_key in ("custom", "default")


def _safe_template(key: str):
    try:
        return get_template(key)
    except ProjectError:
        return None


def _errtext(e: Exception) -> str:
    text = str(e) or repr(e)
    if not isinstance(e, (ProjectError, CatalogError, KeyError, ValueError)):
        text = f"{type(e).__name__}: {text}\n{traceback.format_exc()[-1500:]}"
    return text
