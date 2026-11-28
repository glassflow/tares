"""Who may call: API keys, people, whoami, the cell's capabilities, the enterprise licence and a
project's own keys."""
from __future__ import annotations

import hashlib
import os
import secrets
import uuid

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import factory_home, licence
from .. import providers as providers_mod
from .. import team as team_mod
from ..deps import Deps
from ..providers import resolve_api_base, resolve_provider
from ..slack import resolve_token as resolve_slack_token


def router(deps: Deps) -> APIRouter:
    r = APIRouter()
    store = deps.store
    _err = deps.err
    _current_person = deps.current_person
    _is_admin = deps.is_admin
    _can_see = deps.can_see
    _hidden_ids = deps.hidden_ids
    _project_or_404 = deps.project_or_404

    def _resolve_project(ref: str, default: bool = True) -> str | None:
        """A project id from an id or a name; "" is the default project (or None when `default`
        is off, for an update that leaves the project alone). Unknown answers 400."""
        ref = (ref or "").strip()
        if not ref:
            uid = store.default_project_id() if default else None
            if uid and not _can_see(store.get_project(uid)):
                # a default the caller cannot see is no default for them: say what to do, and
                # never name the project or write into it
                _err(ValueError("name the project to use (its id or name)"), 400)
            return uid
        p = store.get_project(ref) or store.get_project_by_name(ref)
        if p is None or not _can_see(p):   # a private one the caller cannot see is unknown
            _err(ValueError(f"unknown project {ref!r}"), 400)
        return p["id"]

    # ── API keys: scoped, revocable credentials (admin scope; see docs/design) ─
    _SCOPES = {"read", "ingest", "admin"}

    def _key_out(k: dict) -> dict:
        """A key as listed: its person as {id, name}, or null when nobody is bound."""
        p = store.get_person(k["person_id"]) if k.get("person_id") else None
        return {**k, "person": {"id": p["id"], "name": p["name"]} if p else None}

    def _key_hidden(k: dict | None, hidden: set[str]) -> bool:
        """A key bound to a private project the caller cannot see (TR-998): it is not in the list
        and answers a change as a key that is not there."""
        return bool(k and k.get("project") and k["project"] in hidden)

    @r.get("/api/keys")
    def list_keys():
        hidden = _hidden_ids()
        return {"keys": [_key_out(k) for k in store.list_api_keys()
                         if not _key_hidden(k, hidden)],
                "enforced": bool(deps.auth_token),   # without a root auth token the instance is open
                "scopes": sorted(_SCOPES)}

    # A project key (TR-335) reads one project; nothing else (the findings scope went with Tares
    # agents, TR-1810).
    _PROJECT_SCOPES = {"read"}

    def _make_key(name: str, scopes: list[str], project: str | None,
                  person_id: str | None = None) -> dict:
        kid = uuid.uuid4().hex[:8]
        secret = f"nvf_{kid}_{secrets.token_urlsafe(24)}"
        store.insert_api_key(kid, name, f"nvf_{kid}", hashlib.sha256(secret.encode()).hexdigest(),
                             scopes, project=project, person_id=person_id)
        # the secret exists only in this response; the store keeps its hash
        out = {"id": kid, "name": name, "scopes": scopes, "secret": secret}
        if project:
            out["project"] = project
        p = store.get_person(person_id) if person_id else None
        out["person"] = {"id": p["id"], "name": p["name"]} if p else None
        return out

    def _body_person(request: Request, body: dict) -> str | None:
        """The person id a key request names: `person_id` (must exist), or `person: {email, name}`
        (found by email or made: `cloud` when the system credential asks, as the control plane
        does, `key` otherwise). None when it names neither. A person who is not on the cell yet
        needs the licence's seats; the owner (the system credential's `owner: true`) arriving under
        an email nobody has, once `local` is claimed, is `local`: never refused, never a new row."""
        pid = str(body.get("person_id") or "").strip()
        if pid:
            if not store.get_person(pid):
                _err(KeyError(f"no person {pid!r}"), 404)
            return pid
        spec = body.get("person")
        if spec is None:
            return None
        if not isinstance(spec, dict) or not str(spec.get("email") or "").strip():
            _err(ValueError("person needs an email: {email, name?}"))
        ident = getattr(request.state, "credential", None) or {}
        system = ident.get("kind") == "system"
        owner = system and spec.get("owner") is True   # from the control plane only
        try:
            email = store.check_email(spec["email"])
            with team_mod.ADD_LOCK:   # the seat check and the insert are one step
                if email and not store.find_person_by_email(email):
                    local = store.get_person("local")
                    if owner and local and not local["email"] and not local["emails"]:
                        pass   # claims the unclaimed `local` below: not a new row
                    else:
                        ok, why = team_mod.get().can_add_person(store)
                        if not ok:
                            if owner and local:
                                return local["id"]
                            deps.licence_required(why)
                return store.person_or_create(
                    spec["email"], spec.get("name"), via="cloud" if system else "key",
                    owner=owner)["id"]
        except ValueError as e:
            _err(e, 409 if "already belongs" in str(e) else 400)

    @r.post("/api/keys", status_code=201)
    def create_key(request: Request, body: dict = Body(...)):
        """{name, scopes, project?, person_id? | person?: {email, name}}. With `project` (an id
        or a name) it is a project key: scope `read` (the default), over that project only. `person_id` or `person` binds the key to a person (made by email when new)."""
        name = str(body.get("name") or "").strip()
        scopes = sorted(set(body.get("scopes") or []))
        if not name:
            _err(ValueError("name is required"))
        project = str(body.get("project") or "").strip()
        if project:
            uid = _resolve_project(project)
            scopes = scopes or sorted(_PROJECT_SCOPES)
            if not set(scopes) <= _PROJECT_SCOPES:
                _err(ValueError(f"a project key's scopes are a subset of {sorted(_PROJECT_SCOPES)}"))
            return _make_key(name, scopes, uid, _body_person(request, body))
        if not scopes or not set(scopes) <= _SCOPES:
            _err(ValueError(f"scopes must be a non-empty subset of {sorted(_SCOPES)}"))
        return _make_key(name, scopes, None, _body_person(request, body))

    @r.put("/api/keys/{kid}/person")
    def set_key_person(kid: str, request: Request, body: dict = Body(...)):
        """Bind a key to a person: {person_id} or {person: {email, name}}. Admin."""
        if not body.get("person_id") and not body.get("person"):
            _err(ValueError("say which person: {person_id} or {person: {email, name}}"))
        key = store.get_api_key(kid)
        if not key or key["revoked_at"] or _key_hidden(key, _hidden_ids()):
            _err(KeyError(f"no active key {kid!r}"), 404)
        store.set_key_person(kid, _body_person(request, body))
        return _key_out(store.get_api_key(kid))

    @r.delete("/api/keys/{kid}")
    def revoke_key(kid: str):
        if _key_hidden(store.get_api_key(kid), _hidden_ids()) or not store.revoke_api_key(kid):
            _err(KeyError(f"no active key {kid!r}"), 404)
        return {"ok": True, "note": "key revoked"}

    # ── People: who is on the cell. Keys belong to them; the credential names who is calling ──
    def _person_out(p: dict) -> dict:
        return {k: p.get(k) for k in ("id", "name", "email", "emails", "github", "is_admin",
                                      "created_via", "picture")}

    @r.get("/api/people")
    def list_people():
        return {"people": [_person_out(p) for p in store.list_people()]}

    @r.post("/api/people")
    def create_person(request: Request, body: dict = Body(...)):
        """{name, email?, emails?, github?, is_admin?, owner?, picture?}. 201 with the new person;
        200 with the existing one when that email is already someone's. `owner: true` (the system
        credential only, as the control plane sends for the workspace owner) makes an unclaimed
        `local` that person instead (200). `picture` (an https address, TR-1016) is stored on a new
        person; on an existing one only the system credential sets it, as the control plane does
        with the picture a person signs in with, so their own console shows it."""
        email = None
        ident = getattr(request.state, "credential", None) or {}
        system = ident.get("kind") == "system"
        try:
            picture = store.check_picture(body.get("picture")) if "picture" in body else None
            email = store.check_email(body.get("email"))
            def existing(found):
                if system and picture and picture != found.get("picture"):
                    found = store.update_person(found["id"], picture=picture) or found
                return _person_out(found)

            found = store.find_person_by_email(email) if email else None
            if found:
                return existing(found)
            if email and body.get("owner") is True and system:
                claimed = store.claim_local(email, body.get("name"))
                if claimed:
                    if picture:
                        claimed = store.update_person(claimed["id"], picture=picture) or claimed
                    factory_home.clear_cache()   # the kept pages name people
                    return _person_out(claimed)
            with team_mod.ADD_LOCK:   # the seat check and the insert are one step
                # a creation of this email that held the lock first has made the row since the
                # lookup above: this one gets that person (200), not a seat refusal
                found = store.find_person_by_email(email) if email else None
                if found:
                    return existing(found)
                ok, why = team_mod.get().can_add_person(store)
                if not ok:
                    # the owner under another email, once `local` is claimed, is `local`
                    local = store.get_person("local") if email and system and body.get("owner") is True else None
                    if not local:
                        deps.licence_required(why)
                    if picture and picture != local.get("picture"):
                        local = store.update_person("local", picture=picture) or local
                    return _person_out(local)
                p = store.create_person(body.get("name"), email, body.get("emails"), body.get("github"),
                                        bool(body.get("is_admin")), via="admin")
            if picture:
                p = store.update_person(p["id"], picture=picture) or p
            factory_home.clear_cache()
        except ValueError as e:
            # this handler runs in the thread pool: two creations of one email can both pass the
            # lookup above, and the loser then meets the winner's row. It gets that person (200).
            found = store.find_person_by_email(email) if email and "already belongs" in str(e) else None
            if found:
                return _person_out(found)
            # a second creation of the same extra email or GitHub login answers 409
            _err(e, 409 if "already belongs" in str(e) else 400)
        return JSONResponse(_person_out(p), status_code=201)

    @r.delete("/api/people/{pid}")
    def remove_person(pid: str, request: Request):
        """Someone left: an admin, or the control plane when a workspace member is removed. They
        go off the list and the seat count and their keys are revoked; their work keeps their name."""
        if not _is_admin(request):
            raise HTTPException(status_code=403, detail="only an admin can remove a person")
        ident = getattr(request.state, "credential", None) or {}
        if ident.get("kind") != "system" and (_current_person(request) or {}).get("id") == pid:
            raise HTTPException(status_code=400, detail="you cannot remove yourself")
        try:
            removed = store.remove_person(pid)
        except ValueError as e:
            _err(e, 400)
        if not removed:
            raise HTTPException(status_code=404, detail="no such person")
        factory_home.clear_cache()
        return {"removed": True}

    @r.patch("/api/people/{pid}")
    def update_person(pid: str, request: Request, body: dict = Body(...)):
        """An admin (or the system credential) changes anything; a person changes only their own
        name and picture. Emails and the GitHub login are how a person is recognised, so they are
        not self-service."""
        admin = _is_admin(request)
        ident = getattr(request.state, "credential", None) or {}
        kid = str(ident.get("id") or "")
        # themselves = a key explicitly bound to this person; the `local` fallback of an unbound
        # key does not count, or any read key could rename the only person on a one-person cell
        own = kid.startswith("key:") and store.key_person_id(kid[4:]) == pid
        if not admin and not own:
            raise HTTPException(status_code=403, detail="only an admin, or the person themselves, "
                                                        "can change a person")
        if admin and not own and ident.get("kind") != "system" and not licence.ee_allowed():
            # without ee an admin edits themselves; anyone else needs the licence (a person who is
            # not on the cell answers 404 below, as before)
            mine = (_current_person(request) or {}).get("id")
            if mine != pid and store.get_person(pid):
                deps.licence_required("Changing other people needs an enterprise licence.")
        allowed = (("name", "email", "emails", "github", "is_admin", "picture") if admin
                   else ("name", "picture"))
        fields = {k: body[k] for k in allowed if k in body}
        if not admin and any(k in body for k in ("email", "emails", "github", "is_admin")):
            raise HTTPException(status_code=403, detail="only an admin can change a person's "
                                                        "emails, GitHub login or admin flag")
        try:
            p = store.update_person(pid, **fields)
        except ValueError as e:
            _err(e, 409 if "already belongs" in str(e) else 400)
        if not p:
            _err(KeyError(f"no person {pid!r}"), 404)
        factory_home.clear_cache()
        return _person_out(p)

    @r.get("/api/whoami")
    def whoami(request: Request):
        ident = getattr(request.state, "credential", None)
        scopes = getattr(request.state, "scopes", None)
        if ident is None:   # guard not active (open instance) or ingest-path credential
            return {"id": "open", "name": "no auth configured", "kind": "open",
                    "person": _current_person(request), "scopes": sorted(_SCOPES)}
        out = {**ident, "scopes": scopes or []}
        if ident.get("project"):   # a project key: the project it reads, by id and name
            out["project_name"] = (store.get_project(ident["project"]) or {}).get("name")
        return out

    @r.get("/api/capabilities")
    def capabilities():
        """Host capabilities that gate console features, so the UI can hide actions that cannot
        work on this deployment."""
        from importlib.metadata import version as _pkg_version
        try:
            ver = _pkg_version("tares")   # the installed release (release.sh bumps pyproject.toml)
        except Exception:
            ver = None
        url_configured = resolve_api_base(store)[1] != ""
        return {
            "version": ver,
            # whether a model provider resolves (env ANTHROPIC_API_KEY or a saved provider)
            "agent_key_configured": resolve_provider(store)[0] is not None,
            # the summaries and the setup planner run on the cell default
            "default_provider": next(({"id": p["id"], "name": p["name"], "kind": p["kind"]}
                                      for p in providers_mod.list_providers(store)["providers"]
                                      if p["default"]), None),
            "url_configured": url_configured,
            # whether a Slack bot token is configured
            "slack_configured": bool(resolve_slack_token(store)[0]),
        }

    # ── the enterprise licence (TR-912) ──
    # Not under /api/settings: that prefix is admin even to read, and any signed-in person may see
    # whether the cell is licensed. A project key is refused by the allowlist. Writing needs admin.
    # The key text is never returned and never logged.
    def _licence_body(force: bool = False) -> dict:
        return licence.current(store, force=force)

    @r.get("/api/licence")
    def get_licence():
        return _licence_body()

    @r.put("/api/licence")
    def put_licence(body: dict = Body(...)):
        key = body.get("key")
        key = key.strip() if isinstance(key, str) else ""
        if not key:
            _err(ValueError("key is required (DELETE removes the stored enterprise licence)"))
        lic = licence.verify(key)
        if isinstance(lic, licence.Invalid):
            _err(ValueError(lic.reason))
        store.set_setting(licence.LICENCE_SETTING, key)
        print(f"taresd: licence key stored (kid {lic.kid})", flush=True)
        return {**_licence_body(force=True),
                "env_in_use": bool(os.environ.get(licence.LICENCE_ENV, "").strip())}

    @r.delete("/api/licence")
    def delete_licence():
        store.set_setting(licence.LICENCE_SETTING, None)
        print("taresd: licence key removed", flush=True)
        return _licence_body(force=True)

    # ── a project's keys (TR-335) and stats over its sources ─────────────────────────────────
    @r.get("/api/projects/{uid}/keys")
    def list_project_keys(uid: str):
        """The project's active keys, without their secrets."""
        _project_or_404(uid)
        return {"keys": [k for k in store.list_api_keys(project=uid) if not k["revoked_at"]],
                "enforced": bool(deps.auth_token)}

    @r.post("/api/projects/{uid}/keys", status_code=201)
    def create_project_key(uid: str, body: dict = Body(...)):
        """{name}: a key that reads this project only. The secret is
        in this response only."""
        _project_or_404(uid)
        name = str(body.get("name") or "").strip()
        if not name:
            _err(ValueError("name is required"))
        if len(name) > 64:
            _err(ValueError("name is at most 64 characters"))
        return _make_key(name, sorted(_PROJECT_SCOPES), uid)

    return r
