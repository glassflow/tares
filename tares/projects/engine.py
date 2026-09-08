"""The engine turns a template's plan into real objects and keeps them in step with the params.

Every write goes through the catalog importer (`config.import_catalog_dict`), so a project's
objects get exactly the validation, secret handling and runtime start a catalog import gets. What
the engine adds is ownership (`owned_by` on the object rows, `project_objects` mapping plan keys to
names), a diff on update, all-or-nothing create, pause/resume, delete and repair.
"""
from __future__ import annotations

import traceback
import uuid

from ..config import CatalogError, agent_url, import_catalog_dict
from .base import PlannedObject, ProjectError
from .registry import get_template, list_templates as _list_templates

# delete order: dependents first (an agent references a trigger, a trigger a view, a view sources;
# an agent may reference an mcp server)
_DELETE_ORDER = ("agent", "trigger", "view", "source", "mcp_server")
_SECTION = {"source": "sources", "view": "views", "trigger": "triggers",
            "agent": "agents", "mcp_server": "mcp_servers"}


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

    def _existing_names(self) -> dict[str, dict[str, dict]]:
        s = self.store
        return {"source": {x["name"]: x for x in s.list_catalog_sources()},
                "view": {x["name"]: x for x in s.list_catalog_views()},
                "trigger": {x["name"]: x for x in s.list_catalog_triggers()},
                "agent": {x["name"]: x for x in s.list_catalog_agents()},
                "mcp_server": {x["name"]: x for x in s.list_mcp_servers()}}

    def get(self, uid: str) -> dict | None:
        inst = self.store.get_project(uid)
        if inst is None:
            return None
        existing = self._existing_names()
        custom = _is_custom(inst["template"])
        objects = []
        for o in self.store.list_project_objects(uid):
            row = existing[o["kind"]].get(o["name"])
            # a custom project's object that was deleted by hand and recreated under another
            # project is lost to this one: missing, and nothing here acts on it any more
            lost = custom and row is not None and row.get("owned_by") not in (None, uid)
            objects.append({**o, "missing": row is None or lost,
                            "customized": bool(row and row.get("customized")) or o["customized"]})
        template = _safe_template(inst["template"])
        # `recipe` mirrors `template` for pre-1.14 clients; dropped two releases after 1.14
        title = template.title if template else inst["template"]
        return {**inst, "template_title": title, "objects": objects,
                "recipe": inst["template"], "recipe_title": title}

    def list(self) -> list[dict]:
        return [self.get(u["id"]) for u in self.store.list_projects()]

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
    def create(self, template_key: str, params: dict, name: str | None = None) -> dict:
        template = get_template(template_key)
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
        self.store.create_project(uid, template.key, name, params, status="active")
        self.store.log_project(uid, "create", f"{len(plan)} objects planned")
        before = self._existing_names()
        try:
            self._check_ownership(uid, plan, before)
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
            self._delete_objects(created, purge_events=False)
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
        template = get_template(inst["template"])
        params = template.validate(params)
        template.preflight(params, self.store)
        plan = template.plan(params)
        if _is_custom(inst["template"]):
            return self._update_custom(uid, inst, params, plan)
        existing = {(o["kind"], o["key"]): o for o in self.store.list_project_objects(uid)}
        current = self._existing_names()
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
                self._delete_objects([PlannedObject(o.kind, o.key, {"name": prev["name"]})],
                                     purge_events=False)
            report["updated" if row is not None else "created"].append(f"{o.kind}:{o.name}")
            to_apply.append(o)
        planned_keys = {(o.kind, o.key) for o in plan}
        removed = [PlannedObject(k, key, {"name": o["name"]})
                   for (k, key), o in existing.items() if (k, key) not in planned_keys]
        self._check_ownership(uid, to_apply, current)
        self._delete_objects(removed, purge_events=False)
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

    def pause(self, uid: str, sources: bool = False) -> dict:
        """Triggers off, agents unsubscribed; sources keep ingesting unless `sources` is set, in
        which case the project's sources that are running are paused too (polling stops, pushes
        are refused) and remembered, so resume brings back exactly those: a source paused by hand
        before stays paused."""
        inst = self._require(uid)
        objects = self._live_objects(uid)
        if _is_custom(inst["template"]) and inst["status"] != "paused":
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
        custom = _is_custom(inst["template"])
        resume_agents = set(inst["params"].get("resume_agents") or []) if custom else set()
        resume_triggers = set(inst["params"].get("resume_triggers") or []) if custom else set()
        for o in self._live_objects(uid):
            if o["kind"] == "trigger":
                # a custom project's trigger that the user had paused before stays paused
                if not custom or o["name"] in resume_triggers:
                    self.store.set_trigger_paused(o["name"], False)
            elif o["kind"] == "agent":
                spec = plan.get(("agent", o["key"]))
                agent = self.store.get_catalog_agent(o["name"])
                wanted = (o["name"] in resume_agents) if custom else bool(
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
        if custom or paused_sources:
            drop = ("resume_agents", "resume_triggers") if custom else ()
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
               delete_objects: list[tuple[str, str]] | None = None) -> dict:
        """Remove the project. A template project takes its objects with it. A custom project
        releases them, except the ones in `delete_objects` (kind, name), which go too: what the
        builder assembled is the user's to delete from here, in one go. An object in the
        selection whose dependents are not also selected is refused by name, so nothing is left
        pointing at something that no longer exists."""
        inst = self._require(uid)
        objs = [PlannedObject(o["kind"], o["key"], {"name": o["name"]})
                for o in self.store.list_project_objects(uid)]
        if _is_custom(inst["template"]):
            # the objects were there before the project: release them, never delete them. A
            # paused project is resumed first so its triggers and agents are left as they were.
            if inst["status"] == "paused":
                self.resume(uid)
            # purge only what is still this project's: a source deleted by hand and recreated
            # under another project keeps its events
            live = {(o["kind"], o["name"]) for o in self._live_objects(uid)}
            chosen = {(k, n) for k, n in (delete_objects or [])}
            mine = {(o.kind, o.name) for o in objs}
            unknown = chosen - mine
            if unknown:
                raise ProjectError("not this project's objects: "
                                   + ", ".join(f"{k}:{n}" for k, n in sorted(unknown)))
            for k, n in sorted(chosen):
                missing = [d for d in dependents(self.store, k, n)
                           if (d["kind"], d["name"]) not in chosen]
                if missing:
                    raise ProjectError(f"{k} {n!r} is still used by "
                                       + ", ".join(f"{d['kind']} {d['name']}" for d in missing)
                                       + "; delete those too or keep it")
            going = [o for o in objs if (o.kind, o.name) in chosen and (o.kind, o.name) in live]
            self._release(uid, objs)
            purged = self._delete_objects(going, purge_events=purge_events)
            purged += sum(self.store.purge_events(o.name) for o in objs
                          if o.kind == "source" and ("source", o.name) in live
                          and (o.kind, o.name) not in chosen) if purge_events else 0
            self.store.delete_project(uid)
            self._do_reload()
            return {"ok": True, "deleted": [f"{o.kind}:{o.name}" for o in going],
                    "released": [f"{o.kind}:{o.name}" for o in objs if o not in going],
                    "purged_events": purged}
        # a template project takes everything it created unless the user unpicked some of it:
        # those are released and stay behind, unowned (no repair for them any more)
        if delete_objects is None:
            going, kept = objs, []
        else:
            chosen = {(k, n) for k, n in delete_objects}
            unknown = chosen - {(o.kind, o.name) for o in objs}
            if unknown:
                raise ProjectError("not this project's objects: "
                                   + ", ".join(f"{k}:{n}" for k, n in sorted(unknown)))
            for k, n in sorted(chosen):
                missing = [d for d in dependents(self.store, k, n)
                           if (d["kind"], d["name"]) not in chosen]
                if missing:
                    raise ProjectError(f"{k} {n!r} is still used by "
                                       + ", ".join(f"{d['kind']} {d['name']}" for d in missing)
                                       + "; delete those too or keep it")
            going = [o for o in objs if (o.kind, o.name) in chosen]
            kept = [o for o in objs if (o.kind, o.name) not in chosen]
        if inst["status"] == "paused" and kept:
            self.resume(uid)
        self._release(uid, kept)
        purged = self._delete_objects(going, purge_events=purge_events)
        # firings go with the events: a purged project leaves no history behind its triggers
        purged_firings = sum(self.store.purge_dispatches(o.name)
                             for o in going if o.kind == "trigger") if purge_events else 0
        self.store.delete_project(uid)
        self._do_reload()
        return {"ok": True, "deleted": [f"{o.kind}:{o.name}" for o in going],
                "released": [f"{o.kind}:{o.name}" for o in kept],
                "purged_events": purged, "purged_firings": purged_firings}

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
        if _is_custom(inst["template"]):
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
            self._delete_objects([PlannedObject(target.kind, target.key, {"name": prev["name"]})],
                                 purge_events=False)
        self._check_ownership(uid, [target], self._existing_names())
        self._apply(uid, [target])
        self._do_reload()
        self.store.log_project(uid, "repaired", f"{target.kind}:{target.name}")
        return self.get(uid)

    # ── internals ────────────────────────────────────────────────────────────
    def _live_objects(self, uid: str) -> list[dict]:
        """The project's objects that are still its to act on (see get(): a custom project's
        object recreated under another project is missing here)."""
        return [o for o in (self.get(uid) or {}).get("objects", []) if not o["missing"]]

    def _require(self, uid: str) -> dict:
        inst = self.store.get_project(uid)
        if inst is None:
            raise KeyError(f"unknown project {uid!r}")
        return inst

    def _check_ownership(self, uid: str, plan: list[PlannedObject], existing: dict) -> None:
        """A plan may adopt an unowned object of the same name (upsert), never one that belongs to
        another project."""
        for o in plan:
            row = existing[o.kind].get(o.name)
            if row is not None and row.get("owned_by") and row["owned_by"] != uid:
                raise ProjectError(
                    f"{o.kind} {o.name!r} belongs to another project ({row['owned_by']})")

    def _apply(self, uid: str, plan: list[PlannedObject]) -> None:
        if not plan:
            return
        doc: dict = {}
        for o in plan:
            doc.setdefault(_SECTION[o.kind], []).append(dict(o.spec))
        try:
            import_catalog_dict(self.store, doc)   # validates the whole doc, then writes
        except CatalogError as e:
            raise ProjectError(str(e)) from e
        for o in plan:
            self.store.set_owned_by(o.kind, o.name, uid)
            self.store.upsert_project_object(uid, o.kind, o.key, o.name)

    # ── custom projects: adopt and release instead of create and delete ─────
    def _adopt(self, uid: str, objs: list[PlannedObject], existing: dict) -> None:
        """Take ownership of objects that already exist. Checks everything first (the object
        exists and is unowned or ours), then writes; a failure part-way releases what this call
        adopted, so ownership is never left half applied."""
        for o in objs:
            if o.name not in existing[o.kind]:
                raise ProjectError(f"{o.kind} {o.name!r} does not exist")
        self._check_ownership(uid, objs, existing)
        done: list[PlannedObject] = []
        try:
            for o in objs:
                # a conditional claim, not a blind write: were another project to adopt the same
                # object between the check above and here, exactly one of the two wins
                if not self.store.claim_owned_by(o.kind, o.name, uid):
                    raise ProjectError(f"{o.kind} {o.name!r} was just claimed by another project")
                self.store.upsert_project_object(uid, o.kind, o.key, o.name)
                done.append(o)
        except Exception:
            self._release(uid, done)
            raise

    def _release(self, uid: str, objs: list[PlannedObject]) -> None:
        """Drop ownership; the objects stay exactly as they are. Ownership is cleared only where
        the row is still ours: a same-name object recreated by hand may belong elsewhere now."""
        for o in objs:
            self.store.release_owned_by(o.kind, o.name, uid)
            self.store.delete_project_object(uid, o.kind, o.key)

    def _update_custom(self, uid: str, inst: dict, params: dict, plan: list[PlannedObject]) -> dict:
        existing = {(o["kind"], o["key"]): o for o in self.store.list_project_objects(uid)}
        planned = {(o.kind, o.key) for o in plan}
        added = [o for o in plan if (o.kind, o.key) not in existing]
        removed = [PlannedObject(k, key, {"name": o["name"]})
                   for (k, key), o in existing.items() if (k, key) not in planned]
        current = self._existing_names()
        # a kept object that was deleted by hand and recreated under the same name is unowned
        # again: re-adopt it, or another project could claim it while it is still listed here
        reclaim = [o for o in plan if (o.kind, o.key) in existing
                   and (row := current[o.kind].get(o.name)) is not None and not row.get("owned_by")]
        added = added + reclaim
        # validate before the first write: the additions must exist and be free
        for o in added:
            if o.name not in current[o.kind]:
                raise ProjectError(f"{o.kind} {o.name!r} does not exist")
        self._check_ownership(uid, added, current)
        try:
            self._release(uid, removed)
            self._adopt(uid, added, current)
        except Exception as e:
            self._adopt(uid, [o for o in removed if o.name in current[o.kind]], current)
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
        self.store.update_project(uid, params={**keep, **params}, last_error=None,
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

    def _delete_objects(self, objs: list[PlannedObject], purge_events: bool) -> int:
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
                elif kind == "view":
                    s.delete_catalog_view(o.name)
                elif kind == "source":
                    s.delete_catalog_source(o.name)
                    if purge_events:
                        purged += s.purge_events(o.name)
                elif kind == "mcp_server":
                    s.delete_mcp_server(o.name)
        return purged

    def _do_reload(self) -> None:
        if self._reload is not None:
            self._reload()


def dependents(store, kind: str, name: str) -> list[dict]:
    """Everything that stops working if this object goes, in delete order: a source's views, those
    views' triggers, those triggers' agents; a view's triggers and their agents; a trigger's
    agents. Each entry is {kind, name}. Used by the delete dialogs to say what else goes, and by
    the cascade deletes to take it along."""
    views = store.list_catalog_views()
    triggers = store.list_catalog_triggers()
    agents = store.list_catalog_agents()
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(k, n):
        if (k, n) not in seen:
            seen.add((k, n))
            out.append({"kind": k, "name": n})

    view_names = [name] if kind == "view" else []
    if kind == "source":
        view_names = [v["name"] for v in views if name in (v.get("sources") or [])]
    trigger_names = [name] if kind == "trigger" else []
    if view_names:
        trigger_names += [t["name"] for t in triggers if t.get("view") in view_names]
    agent_names = [a["name"] for a in agents if a.get("trigger") in trigger_names]
    for n in agent_names:
        add("agent", n)
    for n in trigger_names:
        if kind != "trigger" or n != name:
            add("trigger", n)
    for n in view_names:
        if kind != "view" or n != name:
            add("view", n)
    return out


def _is_custom(template_key: str) -> bool:
    return template_key == "custom"


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
