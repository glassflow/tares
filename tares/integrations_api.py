"""HTTP routes for the integration slots (TR-842), registered by the daemon.

The software factory has seven slots (tickets, review, ci, memory, monitoring, guardrails,
runtime). The factory picks one integration per slot and gives it integration settings; a project
may pick its own for tickets, review and ci. The choices live in tares/factory_store.py and which
one applies in tares/integrations/resolve.py; this module is the HTTP face of both.

  GET    /api/factory/integrations                  every slot: what is installed, the choice, what applies
  PUT    /api/factory/integrations/{slot}           admin: choose an integration and its integration settings
  POST   /api/factory/integrations/{slot}/test      admin: try an integration without saving
  GET    /api/factory/integrations/effective        what applies per slot, names only (the get_integrations tool)
  GET    /api/projects/{uid}/integrations/{slot}    admin or the project's lead: its own choice and what applies
  PUT    /api/projects/{uid}/integrations/{slot}    admin or the project's lead
  DELETE /api/projects/{uid}/integrations/{slot}    admin or the project's lead: back to the factory's choice
  POST   /api/projects/{uid}/integrations/{slot}/test   admin or the project's lead
  GET    /api/projects/{uid}/required-checks        the commands the local ci integration requires
  PUT    /api/projects/{uid}/required-checks        admin or the project's lead: {commands: [...]}

A request that carries X-Tares-Station (a crew station) may use only the `effective` read, which
never holds integration settings. No response holds a secret value: a read shows each secret as ""
and says in `secrets` whether one is set. A project's settings are never merged with the
factory's, in a write or in a test, so a project cannot make use of a factory secret. A save or a
test that changes the address and leaves a stored secret blank is 422: the old secret never goes
to a new address. A test fills any field it does not post from the stored row.
"""
from __future__ import annotations

import asyncio

from fastapi import Body, HTTPException, Request

from . import licence, outbound
from .factory_api import caller
from .factory_store import FactoryStore, PROJECT_SLOTS
from .integrations import registry, resolve
from .integrations.registry import SLOTS

TEST_SECONDS = 10
MAX_VALUE = 4096


def _field(f) -> dict:
    return {"name": f.name, "label": f.label, "kind": f.kind, "required": bool(f.required),
            "help": f.help, "options": list(f.options)}


def _available(slot: str) -> list[dict]:
    allowed = licence.ee_allowed()
    return [{"name": c.name, "title": c.title or c.name, "ee": bool(c.ee),
             "allowed": (not c.ee) or allowed,
             "settings_schema": [_field(f) for f in c.settings_schema]}
            for c in registry.available(slot)]


def _title(slot: str, name: str) -> str:
    cls = registry.get(slot, name)
    return (cls.title or name) if cls else name


def register(app, store, h):
    def refuse(code: int, message: str):
        raise HTTPException(status_code=code, detail=message)

    def no_station(request: Request):
        if caller(request)["station"]:
            refuse(403, "a crew station reads integrations through the get_integrations tool")

    def slot_of(slot: str, project: bool = False) -> str:
        if project:
            if slot not in PROJECT_SLOTS:
                refuse(422, f"a project can choose its own for {', '.join(PROJECT_SLOTS)} only "
                            f"(got {slot!r})")
        elif slot not in SLOTS:
            refuse(404, f"there is no slot {slot!r}; the slots are {', '.join(SLOTS)}")
        return slot

    def may_write_project(request: Request, p: dict) -> None:
        """An admin, or the person who leads the project."""
        if h.is_admin(request):
            return
        me = h.current_person(request)
        if me and me["id"] == p.get("lead_person_id"):
            return
        refuse(403, "this needs an admin or the project's lead")

    def choice_of(slot: str, uid: str | None):
        """The row that decides the slot for this scope, redacted: the project's own when it has
        one, else the factory's."""
        row = store.get_project_choice(uid, slot) if uid and slot in PROJECT_SLOTS else None
        return row or store.get_choice(slot)

    def applying(slot: str, uid: str | None) -> dict:
        """What applies to the slot. The reason a stored default is "not installed" is left out:
        nothing ships built in yet, so a cell that picked the default would be told it is missing."""
        row = choice_of(slot, uid)
        eff = resolve.effective(slot, uid, store=store)
        reason = eff.reason
        if reason == resolve.NOT_INSTALLED and row and row["name"] == resolve.DEFAULTS[slot]:
            reason = ""
        return {"integration": eff.integration, "title": _title(slot, eff.integration),
                "source": eff.source, "reason": reason, "settings": eff.settings}

    def entry(slot: str, uid: str | None = None) -> dict:
        row = store.get_project_choice(uid, slot) if uid else store.get_choice(slot)
        return {"slot": slot, "available": _available(slot), "choice": row,
                "effective": applying(slot, uid)}

    # ── validation of a write or a test ──
    def checked(slot: str, body: dict, uid: str | None, test: bool = False):
        """(class, name, settings, stored, schema) for a request body, or a 422 naming what is
        wrong. The settings are the posted ones reconciled with this scope's own stored row of the
        same integration (a blank secret keeps the stored one). A save replaces the non-secret
        fields; a test (`test=True`) takes the stored ones for any field it does not post, so it
        tries what is saved. Either way a changed address with a secret left blank is refused: the
        old secret never goes to a new address."""
        name = str(body.get("name") or "").strip()
        if not name:
            refuse(422, "name is required: the integration to use")
        posted = body.get("settings")
        if posted is None:
            posted = {}
        if not isinstance(posted, dict):
            refuse(422, "settings must be an object")
        cls = registry.get(slot, name)
        if cls is None:
            refuse(422, f"{name!r} is not installed for {slot}; "
                        f"installed: {', '.join(c.name for c in registry.available(slot)) or 'none'}")
        if cls.ee and not licence.ee_allowed():
            h.licence_required(f"{cls.title or name} needs an enterprise licence")
        schema = {f.name: f for f in cls.settings_schema}
        for k, v in posted.items():
            f = schema.get(k)
            if f is None:
                refuse(422, f"{name} has no integration setting {k!r}; "
                            f"it takes {', '.join(schema) or 'none'}")
            _kind_ok(name, f, v)
        stored = store.stored_settings(slot, name, uid)
        secret = {f.name for f in cls.settings_schema if f.kind == "secret"}
        moved = [f for f in cls.settings_schema if f.kind == "url"
                 and (posted.get(f.name) or "") not in ("", stored.get(f.name))]
        carried = [f for f in cls.settings_schema if f.kind == "secret"
                   and posted.get(f.name, "") == "" and stored.get(f.name) not in (None, "")]
        if moved and carried:
            refuse(422, f"re-enter the secret when changing the address: {carried[0].name} "
                        f"is stored for the old {moved[0].name}")
        try:
            merged = FactoryStore._merge_integration_settings(secret, posted, stored)
        except ValueError as e:
            refuse(422, str(e))
        if test:
            kept = {k: v for k, v in stored.items() if k in schema and k not in secret}
            merged = {**kept, **merged}
        return cls, name, merged, stored, schema

    def _kind_ok(name: str, f, v) -> None:
        """A posted value fits its field's kind (None clears a secret). 422 names the field."""
        if v is None and f.kind == "secret":
            return
        bad = None
        if f.kind == "bool":
            bad = None if isinstance(v, bool) else "true or false"
        elif f.kind == "mapping":
            if not isinstance(v, dict):
                bad = "an object"
            elif f.options and set(v) - set(f.options):
                bad = f"an object with keys from {', '.join(f.options)}"
            elif any(not isinstance(x, str) for x in v.values()):
                bad = "an object of text values"
        elif f.kind == "select":
            if not isinstance(v, str) or (f.options and v not in f.options and v != ""):
                bad = f"one of {', '.join(f.options)}"
        elif not isinstance(v, str):
            bad = "text"
        elif len(v) > MAX_VALUE:
            bad = f"at most {MAX_VALUE} characters"
        if bad:
            refuse(422, f"{f.name} must be {bad}")

    def required_ok(schema: dict, merged: dict) -> None:
        for f in schema.values():
            if f.required and merged.get(f.name) in (None, "", {}):
                refuse(422, f"{f.name} is required ({f.label})")

    # ── what is installed and chosen ──
    @app.get("/api/factory/integrations")
    def list_integrations(request: Request):
        no_station(request)
        licence.current(store)         # ee_allowed() is False until the first check
        return {"slots": [entry(slot) for slot in SLOTS]}

    @app.get("/api/factory/integrations/effective")
    def effective_integrations(request: Request, project: str = ""):
        """Every slot's integration, by name and title, for the factory or for one project. No
        integration settings. A crew station may read this."""
        licence.current(store)
        uid = None
        if project.strip():
            uid = h.project_or_404(h.resolve_project(project.strip()))["id"]
        out = []
        for slot in SLOTS:
            a = applying(slot, uid)
            out.append({"slot": slot, "integration": a["integration"], "title": a["title"],
                        "source": a["source"], "reason": a["reason"]})
        return {"project": uid, "slots": out}

    @app.put("/api/factory/integrations/{slot}")
    def put_integration(slot: str, request: Request, body: dict = Body(...)):
        no_station(request)
        h.require_admin(request)
        slot_of(slot)
        licence.current(store)
        _, name, merged, _, schema = checked(slot, body, None)
        required_ok(schema, merged)
        store.set_choice(slot, name, body.get("settings"), h.by(request, body))
        return entry(slot)

    # ── the connection test ──
    async def run_test(request: Request, slot: str, body: dict, uid: str | None):
        licence.current(store)
        cls, name, merged, _, schema = checked(slot, body, uid, test=True)
        required_ok(schema, merged)
        urls = [f for f in cls.settings_schema if f.kind == "url"]
        secrets = [f for f in cls.settings_schema if f.kind == "secret"]
        for f in urls:
            v = merged.get(f.name)
            if not v:
                continue
            try:
                outbound.valid_url(v)
                await outbound.refuse_internal(v)
            except ValueError as e:
                refuse(422, f"{f.name}: {e}")
        try:
            ok, message = await asyncio.wait_for(cls().check_connection(merged), TEST_SECONDS)
        except asyncio.TimeoutError:
            ok, message = False, f"no answer within {TEST_SECONDS} seconds"
        except Exception as e:         # a test never fails the request
            ok, message = False, f"the test failed: {type(e).__name__}"
        message = str(message or "")
        for f in secrets:              # a message must not echo a secret it was given
            v = merged.get(f.name)
            if isinstance(v, str) and len(v) >= 4:
                message = message.replace(v, "***")
        return {"ok": bool(ok), "message": message[:1000]}

    @app.post("/api/factory/integrations/{slot}/test")
    async def test_integration(slot: str, request: Request, body: dict = Body(...)):
        no_station(request)
        h.require_admin(request)
        slot_of(slot)
        return await run_test(request, slot, body, None)

    # ── a project's own choice ──
    def project_of(uid: str, request: Request, slot: str, write: bool) -> dict:
        no_station(request)
        p = h.project_or_404(h.resolve_project(uid))
        slot_of(slot, project=True)
        if write:
            may_write_project(request, p)
        return p

    @app.get("/api/projects/{uid}/integrations/{slot}")
    def get_project_integration(uid: str, slot: str, request: Request):
        p = project_of(uid, request, slot, True)     # settings are for an admin or the lead
        licence.current(store)
        return entry(slot, p["id"])

    @app.put("/api/projects/{uid}/integrations/{slot}")
    def put_project_integration(uid: str, slot: str, request: Request, body: dict = Body(...)):
        p = project_of(uid, request, slot, True)
        licence.current(store)
        _, name, merged, _, schema = checked(slot, body, p["id"])
        required_ok(schema, merged)
        store.set_project_choice(p["id"], slot, name, body.get("settings"), h.by(request, body))
        return entry(slot, p["id"])

    @app.delete("/api/projects/{uid}/integrations/{slot}")
    def delete_project_integration(uid: str, slot: str, request: Request):
        p = project_of(uid, request, slot, True)
        cleared = store.clear_project_choice(p["id"], slot)
        licence.current(store)
        return {**entry(slot, p["id"]), "cleared": cleared}

    # ── the commands the local ci integration requires ──
    @app.get("/api/projects/{uid}/required-checks")
    def get_required_checks(uid: str, request: Request):
        p = h.project_or_404(h.resolve_project(uid))
        return {"project": p["id"], "commands": store.get_required_checks(p["id"])}

    @app.put("/api/projects/{uid}/required-checks")
    def put_required_checks(uid: str, request: Request, body: dict = Body(...)):
        no_station(request)
        p = h.project_or_404(h.resolve_project(uid))
        may_write_project(request, p)
        try:
            commands = store.set_required_checks(p["id"], body.get("commands"),
                                                 h.by(request, body))
        except ValueError:
            refuse(422, "commands is a list of command strings")
        return {"project": p["id"], "commands": commands}

    @app.post("/api/projects/{uid}/integrations/{slot}/test")
    async def test_project_integration(uid: str, slot: str, request: Request,
                                       body: dict = Body(...)):
        p = project_of(uid, request, slot, True)
        return await run_test(request, slot, body, p["id"])
