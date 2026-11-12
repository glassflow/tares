"""HTTP routes for the crew's guardrail rules (TR-862), registered by the daemon.

The built-in guardrails integration (tares/integrations/builtin/guardrails_tares.py) keeps the rules a
crew station runs under as readable data: the defaults Tares ships, your own deny rules and your allow
rules, which are the grants.

  GET    /api/factory/guardrails/rules                       admin: the defaults and every rule of your own
  GET    /api/factory/guardrails/rules?station=<name>&project=<uid>
                                                             the allow and soft_deny sentences that station
                                                             runs under (an admin or a crew station)
  POST   /api/factory/guardrails/rules                       add a deny rule (an admin)
  PUT    /api/factory/guardrails/rules/{id}                  change the text of a deny rule of your own
  DELETE /api/factory/guardrails/rules/{id}                  remove a deny rule of your own

A default can be neither added over, changed nor removed: any write that names one is 403. An allow
rule is a grant; it is read in place from the grants (GET /api/grants) and written there, so a write
here that asks for one is 422 and points to the grants. The sentences keep a `{name}` placeholder
for what only the station's machine knows (the person's name, the crew's folder); `{prod_pattern}`
is filled from the crew's settings.
"""
from __future__ import annotations

import re

from fastapi import Body, HTTPException, Request

from . import factory_setup
from .factory_api import caller, crew_settings
from .integrations.builtin import guardrails_tares as rules_mod

MAX_TEXT = 500
_GRANT_ID = re.compile(r"^G\d+$", re.I)


def infer_role(station: str) -> str:
    """The role a station name says it plays (a role name, `<crew>-reviewer`, `<project>-build-<slug>`), or ""."""
    n = str(station or "").strip().lower()
    for role in rules_mod.ROLES:
        if n == role or n.endswith("-" + role):
            return role
    return "builder" if "-build-" in n else ""


def register(app, store, h):
    def refuse(code: int, message: str):
        raise HTTPException(status_code=code, detail=message)

    def no_station(request: Request):
        if caller(request)["station"]:
            refuse(403, "a crew station reads its rules with ?station=<its name>; it cannot change them")

    def text_of(body: dict) -> str:
        text = " ".join(str(body.get("text") or "").split())
        if not text:
            refuse(422, "say the rule in a sentence: text is empty")
        if len(text) > MAX_TEXT:
            refuse(422, f"the rule is {len(text)} characters; keep it to {MAX_TEXT}")
        return text

    def project_of(ref) -> dict:
        return h.project_or_404(h.resolve_project(str(ref).strip()))

    def may_write(request: Request) -> None:
        """Writing a rule is an admin's call; a station and a person's own key may not."""
        no_station(request)
        h.require_admin(request)

    def refuse_default(rule_id) -> None:
        if rules_mod.default_rule(rule_id):
            refuse(403, f"{rule_id} is a Tares default: defaults can be read, not changed or removed")

    def own_out(r: dict) -> dict:
        return {"id": r["id"], "scope": r["scope"], "project_uid": r["project"], "kind": r["kind"],
                "text": r["text"], "source": "own", "created_by": r["created_by"],
                "created_at": r["created_at"]}

    def grants_out(project_uid: str | None) -> list[dict]:
        """The grants in effect that are allow rules: the team's, and the project's own with
        `project_uid`. Personal grants hold on their holder's machine and are not listed."""
        out = []
        for g in store.all_grant_rows():
            if g["state"] != "active":
                continue
            if g["kind"] == "team":
                scope, uid = "factory", None
            elif g["kind"] == "project" and project_uid and g["project"] == project_uid:
                scope, uid = "project", g["project"]
            elif g["kind"] == "project" and project_uid is None:
                scope, uid = "project", g["project"]
            else:
                continue
            out.append({"id": g["id"], "scope": scope, "project_uid": uid, "kind": "allow",
                        "text": g["words"], "source": "grant", "given_at": g["given_at"]})
        return out

    def role_for(request: Request, station: str, said: str) -> str:
        said = said.strip().lower()
        if said:
            if said not in rules_mod.ROLES:
                refuse(422, f"role is one of {', '.join(rules_mod.ROLES)} (got {said!r})")
            return said
        me = caller(request)
        if me["station"] == station:
            role = me["role"] if me["role"] in rules_mod.ROLES else infer_role(me["parent"])
            if role:
                return role
        role = infer_role(station)
        if not role:
            refuse(422, f"cannot tell the role of {station!r}: add role="
                        f"{'|'.join(rules_mod.ROLES)}")
        return role

    def rendered(request: Request, station: str, project: str, role: str) -> dict:
        if not caller(request)["station"] and not h.is_admin(request):
            refuse(403, "this needs an admin or a crew station")
        p = project_of(project) if project.strip() else None
        uid = p["id"] if p else None
        role = role_for(request, station, role)
        own = [r for r in store.list_guardrail_rules()
               if r["kind"] == "deny" and (r["scope"] == "factory" or (uid and r["project"] == uid))]
        grants = []
        for g in store.all_grant_rows():
            if g["state"] != "active":
                continue
            if g["kind"] == "team":
                grants.append({**g, "scope": "all projects"})
            elif g["kind"] == "project" and uid and g["project"] == uid:
                grants.append({**g, "scope": f"project {p['name']}"})
        prod = crew_settings(store).get("prod_pattern") or factory_setup.PROD_PATTERN
        out = rules_mod.render(role, allow_grants=[{"id": g["id"], "words": g["words"],
                                                    "given_at": g["given_at"], "scope": g["scope"]}
                                                   for g in grants],
                               deny_rules=own, values={"prod_pattern": prod})
        return {"station": station, "role": role, "project": uid, **out}

    @app.get("/api/factory/guardrails/rules")
    def get_rules(request: Request, station: str = "", project: str = "", role: str = ""):
        """No `station`: the defaults and every rule of your own (an admin only). With `station`:
        the sentences that station runs under, for `project` when one is named."""
        if station.strip():
            return rendered(request, station.strip(), project, role)
        no_station(request)
        h.require_admin(request)
        uid = project_of(project)["id"] if project.strip() else None
        own = [own_out(r) for r in store.list_guardrail_rules()
               if uid is None or r["scope"] == "factory" or r["project"] == uid]
        return {"defaults": [{**d, "source": "default"} for d in rules_mod.DEFAULTS],
                "rules": [*grants_out(uid), *own]}

    @app.post("/api/factory/guardrails/rules", status_code=201)
    def add_rule(request: Request, body: dict = Body(...)):
        may_write(request)
        refuse_default(body.get("id"))
        kind = str(body.get("kind") or "deny").strip().lower()
        if kind == "allow":
            refuse(422, "an allow rule is a grant: add it with add_grant (POST /api/grants)")
        if kind != "deny":
            refuse(422, "kind is deny (an allow rule is a grant)")
        project_ref = str(body.get("project_uid") or body.get("project") or "").strip()
        scope = str(body.get("scope") or ("project" if project_ref else "factory")).strip().lower()
        if scope not in ("factory", "project"):
            refuse(422, "scope is factory or project")
        if scope == "factory" and project_ref:
            refuse(422, "a factory rule has no project; use scope project for one")
        p = None
        if scope == "project":
            if not project_ref:
                refuse(422, "name the project the rule is for")
            p = project_of(project_ref)
        text = text_of(body)
        uid = p["id"] if p else None
        twin = next((r for r in store.list_guardrail_rules()
                     if (r["scope"], r["project"], r["kind"], r["text"]) == (scope, uid, "deny", text)), None)
        if twin:
            return {"added": False, "rule": own_out(twin)}
        row = store.add_guardrail_rule(scope, uid, "deny", text, h.by(request, body))
        return {"added": True, "rule": own_out(row)}

    def rule_or_error(rule_id: str) -> dict:
        refuse_default(rule_id)
        if _GRANT_ID.match(rule_id.strip()):
            refuse(422, f"{rule_id} is a grant (an allow rule): change it with the grants "
                        "(POST /api/grants/{id}/revoke)")
        r = store.get_guardrail_rule(rule_id)
        if r is None:
            refuse(404, f"there is no rule {rule_id}")
        return r

    @app.put("/api/factory/guardrails/rules/{rule_id}")
    def put_rule(rule_id: str, request: Request, body: dict = Body(...)):
        may_write(request)
        r = rule_or_error(rule_id)
        return {"rule": own_out(store.set_guardrail_rule_text(r["id"], text_of(body)))}

    @app.delete("/api/factory/guardrails/rules/{rule_id}")
    def delete_rule(rule_id: str, request: Request):
        may_write(request)
        r = rule_or_error(rule_id)
        return {"deleted": store.delete_guardrail_rule(r["id"]), "id": r["id"]}
