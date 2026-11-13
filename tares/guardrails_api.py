"""HTTP routes for the crew's guardrail rules (TR-862) and guardrail pairs (TR-863), registered by the daemon.

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
  GET    /api/factory/stations/{station}/launch              how that station starts (TR-867), see below

A default can be neither added over, changed nor removed: any write that names one is 403. An allow
rule is a grant; it is read in place from the grants (GET /api/grants) and written there, so a write
here that asks for one is 422 and points to the grants. The sentences keep a `{name}` placeholder
for what only the station's machine knows (the person's name, the crew's folder); `{prod_pattern}`
is filled from the crew's settings.

How a station starts (TR-867): the cell builds the launch spec through the runtime integration (slot
`runtime`, `claude_code` unless another is chosen) and answers it as {runtime, role, command, env, settings,
placeholders}. The `tares factory` command line fills the placeholders (`{CREW_HOME}`, `{PLUGIN_DIR}`,
`{PLUGIN_DIR_SHELL}`, `{WT_REPOS}`, `{REPOS}`, `{TOKEN_FILE}`, `{SETTINGS_FILE}`: what only its machine knows),
adds the boot prompt, `--resume` and the `claude` binary, and starts the session. No answer holds a secret.
An admin key, or the station's own (X-Tares-Station: <station>), may ask; a key that names another station is
403. The station's role, parent and project come from the cell's record of it when it has one; the query
parameters give what only the command line knows, and win over the record:
  role, parent, parent_role (the role a helper works under), project (name or id), repo (the folder, with
  forward slashes), orchestrator (the crew's orchestrator station), human, authorized_at, autonomy (the
  station's own level when it differs from the crew's), cluster, model, effort, autocompact, tares_url,
  mcp_url, token_file (true when the crew keeps its key in a file).
A role the runtime cannot run, or a station with no role to find, is 422.

A guardrail pair is a hook point (`pre_build`, `pre_merge`, `pre_release`) paired with a guardrail
integration and its integration settings; `on_fail` is `block` (the default) or `warn`. The factory
has pairs for every project, and a project adds its own. The hook runner (tares/integrations/hooks.py)
runs them.

  GET    /api/factory/guardrails/pairs                       admin: every factory pair
  POST   /api/factory/guardrails/pairs                       admin: add one, or change one with `id`
  DELETE /api/factory/guardrails/pairs/{id}                  admin: remove a pair, a project's too
  GET    /api/projects/{uid}/guardrails/pairs                admin or the project's lead: its own pairs, and
                                                             the factory's, which show no integration settings
  POST   /api/projects/{uid}/guardrails/pairs                admin or the project's lead: add or change its own
  DELETE /api/projects/{uid}/guardrails/pairs/{id}           admin or the project's lead: remove its own pair

A crew station may use none of these. No answer holds a secret: each shows as "" and `secrets` says
whether one is set. A POST with `id` changes that pair (a blank secret keeps the stored one, JSON null
clears it); without `id` it adds one and answers 201, or `added` false when an identical pair exists.
A project pair keeps its own secrets and never uses a factory pair's. A project cannot remove a
factory pair (403), and a pair of another project is 404 there.
"""
from __future__ import annotations

import re

from fastapi import Body, HTTPException, Request, Response

from . import factory_setup, licence
from .factory_api import caller, crew_settings
from .factory_store import FactoryStore
from .integrations import hooks, registry, resolve
from .integrations.builtin import guardrails_tares as rules_mod
from .integrations_api import kind_ok, may_write_project_of, required_ok

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

    # ── how a station starts (TR-867) ──
    def station_record(station: str, crew: str) -> dict:
        """The newest session the cell has seen play `station` (in `crew` when it is named): its role,
        parent and project. {} for a station the cell has not seen yet, which is every first start."""
        names = {c["id"]: c["name"] for c in store.list_crews()} if crew else {}
        for row in store.station_sessions(h.cc_source()):
            if row["station"] == station and (not crew or names.get(row["crew_id"]) == crew):
                return row
        return {}

    @app.get("/api/factory/stations/{station}/launch")
    def get_launch(station: str, request: Request, role: str = "", parent: str = "", parent_role: str = "",
                   project: str = "", repo: str = "", crew: str = "", orchestrator: str = "", human: str = "",
                   authorized_at: str = "", autonomy: str = "", cluster: str = "", model: str = "",
                   effort: str = "", autocompact: str = "", tares_url: str = "", mcp_url: str = "",
                   token_file: bool = False):
        """The launch spec of `station` (see the module's note): the runtime's command, env and settings
        with placeholders for the command line's own machine. An admin, or a key that carries the station's
        name; the name is not otherwise checked here (TR-870 validates it)."""
        station = station.strip()
        me = caller(request)
        if me["station"]:
            if me["station"] != station:
                refuse(403, f"this key is for station {me['station']}; it cannot read the launch of {station}")
        elif not h.is_admin(request):
            refuse(403, "this needs an admin or the crew station itself")
        crew = crew.strip() or request.headers.get("x-tares-crew", "").strip()
        have = station_record(station, crew)
        role = (role or have.get("role") or infer_role(station)).strip().lower()
        if not role:
            refuse(422, f"cannot tell the role of {station!r}: add role=orchestrator|reviewer|releaser|builder|helper")
        parent = parent or have.get("parent") or ""
        named = project.strip()
        project = named or (have.get("project") or "")
        p = None
        if project:
            try:
                p = h.project_or_404(h.resolve_project(project))
            except HTTPException:
                if named:
                    raise   # a project the caller names must exist, as on the rules route (404)
                p = None   # the station's recorded project may be gone: launch without its rules
        project_id = p["id"] if p else ""
        project_name = p["name"] if p else project
        work_role = role
        if role == "helper":
            work_role = parent_role.strip().lower() or infer_role(parent)
            if work_role not in rules_mod.ROLES:
                refuse(422, "a helper works under its parent's role: add parent_role="
                            "orchestrator|reviewer|releaser|builder")
        rules = rendered(request, station, project_id, work_role)
        eff = resolve.effective("runtime", None, store=store)
        cls = registry.get("runtime", eff.integration)
        if cls is None:
            refuse(422, f"the runtime {eff.integration!r} is not installed")
        mon = resolve.effective("monitoring", None, store=store)
        mon_cls = registry.get("monitoring", mon.integration)
        base = str(request.base_url).rstrip("/")
        person = h.current_person(request)
        ctx = {
            "crew": crew or "crew", "orchestrator": orchestrator.strip(), "parent": parent, "parent_role": work_role,
            "project": project_name, "project_id": project_id, "repo": repo, "human": human,
            "authorized_at": authorized_at, "autonomy": autonomy.strip(),
            "crew_autonomy": crew_settings(store, person, h.is_system(request)).get("autonomy") or "",
            "model": model, "effort": effort, "autocompact": autocompact, "cluster": cluster,
            "tares_url": tares_url.strip() or base, "mcp_url": mcp_url.strip() or f"{base}/mcp",
            "token_file": token_file,
            "rules": {"allow": rules["allow"], "soft_deny": rules["soft_deny"]},
            "monitoring": mon_cls(store) if mon_cls else None,
        }
        try:
            spec = cls().launch_spec(role, station, ctx)
        except ValueError as e:
            refuse(422, str(e))
        return {"runtime": eff.integration, "station": station, "role": role, "command": spec.command,
                "env": spec.env, "settings": spec.settings, "placeholders": spec.placeholders}

    # ── guardrail pairs (TR-863) ──
    def no_station_pairs(request: Request):
        if caller(request)["station"]:
            refuse(403, "a crew station cannot read or change guardrail pairs")

    def factory_only(request: Request):
        no_station_pairs(request)
        h.require_admin(request)

    def project_pairs_of(uid: str, request: Request) -> dict:
        no_station_pairs(request)
        p = project_of(uid)
        may_write_project_of(h, request, p)
        return p

    def checked_pair(body: dict, existing: dict | None):
        """(hook, integration, on_fail, posted settings) for a request body, or a 422 naming what
        is wrong. With `existing` (the pair being changed) a field the body leaves out keeps its
        value; for a new pair `hook` and `integration` are required and `on_fail` is block."""
        def said(key: str, fallback):
            v = body.get(key)
            if v is None and (existing is not None or key == "on_fail"):
                return fallback
            return str(v if v is not None else "").strip()

        hook = said("hook", existing and existing["hook"])
        if hook not in hooks.HOOK_POINTS:
            refuse(422, f"hook must be one of {', '.join(hooks.HOOK_POINTS)}"
                        + (f" (got {hook!r})" if hook else ""))
        name = said("integration", existing and existing["integration"])
        if not name:
            refuse(422, "integration is required: the guardrail integration to run")
        on_fail = said("on_fail", existing["on_fail"] if existing else "block").lower()
        if on_fail not in ("block", "warn"):
            refuse(422, f"on_fail must be block or warn (got {on_fail!r})")
        posted = body.get("settings")
        if posted is None:
            posted = {}
        if not isinstance(posted, dict):
            refuse(422, "settings must be an object")
        licence.current(store)
        cls = registry.get("guardrails", name)
        if cls is None:
            refuse(422, f"{name!r} is not installed for guardrails; installed: "
                        f"{', '.join(c.name for c in registry.available('guardrails')) or 'none'}")
        if cls.ee and not licence.ee_allowed():
            h.licence_required(f"{cls.title or name} needs an enterprise licence")
        schema = {f.name: f for f in cls.settings_schema}
        for k, v in posted.items():
            f = schema.get(k)
            if f is None:
                refuse(422, f"{name} has no integration setting {k!r}; "
                            f"it takes {', '.join(schema) or 'none'}")
            kind_ok(refuse, f, v)
        stored = existing["settings"] if existing and existing["integration"] == name else {}
        # the old secret never goes to a new address (the same refusal as the slot's settings)
        moved = [f for f in cls.settings_schema if f.kind == "url"
                 and (posted.get(f.name) or "") not in ("", stored.get(f.name))]
        carried = [f for f in cls.settings_schema if f.kind == "secret"
                   and posted.get(f.name, "") == "" and stored.get(f.name) not in (None, "")]
        if stored and moved and carried:
            refuse(422, f"re-enter the secret when changing the address: {carried[0].name} "
                        f"is stored for the old {moved[0].name}")
        try:
            merged = FactoryStore._merge_integration_settings(
                {f.name for f in cls.settings_schema if f.kind == "secret"}, posted, stored)
        except ValueError as e:
            refuse(422, str(e))
        required_ok(refuse, schema, merged)
        return hook, name, on_fail, posted

    def save_pair(request: Request, response: Response, body: dict, scope: str, uid: str | None) -> dict:
        pid = str(body.get("id") or "").strip().upper()
        existing = None
        if pid:
            existing = store.guardrail_pair_stored(pid)
            if existing is None or (existing["scope"], existing["project_uid"]) != (scope, uid):
                refuse(404, f"there is no {'factory' if scope == 'factory' else 'project'} pair {pid}")
        hook, name, on_fail, posted = checked_pair(body, existing)
        try:
            pair, how = store.save_guardrail_pair(scope, uid, hook, name, posted, on_fail,
                                                  h.by(request, body), pair_id=pid or None)
        except LookupError:
            refuse(404, f"there is no pair {pid}")
        except ValueError as e:
            refuse(422, str(e))
        if how == "added":
            response.status_code = 201
        return {"added": how == "added", **({"updated": True} if how == "updated" else {}),
                "pair": pair}

    def read_only(pair: dict) -> dict:
        """A factory pair as a project sees it: what runs and what a failure means, no settings."""
        return {**pair, "settings": {}, "secrets": {}, "created_by": None}

    @app.get("/api/factory/guardrails/pairs")
    def get_pairs(request: Request):
        factory_only(request)
        return {"pairs": store.list_guardrail_pairs("factory")}

    @app.post("/api/factory/guardrails/pairs", status_code=200)
    def add_pair(request: Request, response: Response, body: dict = Body(...)):
        factory_only(request)
        return save_pair(request, response, body, "factory", None)

    @app.delete("/api/factory/guardrails/pairs/{pair_id}")
    def delete_pair(pair_id: str, request: Request):
        factory_only(request)
        pair = store.get_guardrail_pair(pair_id)
        if pair is None:
            refuse(404, f"there is no pair {pair_id}")
        return {"deleted": store.delete_guardrail_pair(pair["id"]), "id": pair["id"]}

    @app.get("/api/projects/{uid}/guardrails/pairs")
    def get_project_pairs(uid: str, request: Request):
        p = project_pairs_of(uid, request)
        return {"pairs": [*(read_only(x) for x in store.list_guardrail_pairs("factory")),
                          *store.list_guardrail_pairs("project", p["id"])]}

    @app.post("/api/projects/{uid}/guardrails/pairs", status_code=200)
    def add_project_pair(uid: str, request: Request, response: Response, body: dict = Body(...)):
        p = project_pairs_of(uid, request)
        return save_pair(request, response, body, "project", p["id"])

    @app.delete("/api/projects/{uid}/guardrails/pairs/{pair_id}")
    def delete_project_pair(uid: str, pair_id: str, request: Request):
        p = project_pairs_of(uid, request)
        pair = store.get_guardrail_pair(pair_id)
        if pair is not None and pair["scope"] == "factory":
            refuse(403, f"{pair['id']} is a factory pair: only an admin removes it, from the factory's pairs")
        if pair is None or pair["project_uid"] != p["id"]:
            refuse(404, f"there is no pair {pair_id} in this project")
        return {"deleted": store.delete_guardrail_pair(pair["id"]), "id": pair["id"]}
