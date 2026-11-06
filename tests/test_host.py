"""The daemon's route helpers as one importable class, tares.host.Host (TR-910).

A daemon with auth on. Code that only has the host (app.state.host) adds a route that uses
h.current_person, h.is_admin, h.require_admin and h.err. A person key (ingest scope, no admin)
gets 403 from require_admin, an admin key 200, and a call with no key 401 from the guard. The class
lists every member of the contract, Host(...) keeps its store and runtime, licence_state() is the
cell's licence check (TR-912) with `used` counting the people on the cell, and licence_required()
raises the error the daemon answers with 402.

Run: .venv/bin/python tests/test_host.py
"""
import asyncio
import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-host-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-host"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx
from fastapi import Request

import _people

from tares.host import Host
from tares.licence import LicenceRequired

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


MEMBERS = [
    "err", "caller", "current_person", "is_admin", "is_system", "project_key", "start_who", "by",
    "require_admin", "can_see", "hidden_ids", "read_exclude", "hidden_sources", "project_or_404",
    "resolve_project", "cc_source", "global_ids", "ensure_globals", "ticket_out", "tickets_out",
    "area_owner", "hidden_project", "projects", "licence_state", "licence_required",
]

STATE_KEYS = {"valid", "source", "kid", "org", "seats", "used", "features", "expires", "days_left",
              "expired", "over_seats", "ee_allowed", "reason", "stored", "env_set"}


def add_route(h, app):
    """A module that gets only the host: one POST route that checks the caller through it. The
    path has two segments after /ingest, so the guard asks for the ingest scope and no more."""
    @app.post("/ingest/host-check/who")
    def who(request: Request):
        h.require_admin(request)
        person = h.current_person(request)
        return {"person": (person or {}).get("email"), "admin": h.is_admin(request),
                "system": h.is_system(request), "caller_is_request": bool(h.caller() is not None and h.caller().url.path == request.url.path
                                  and h.caller().headers.get("authorization") == request.headers.get("authorization"))}

    @app.post("/ingest/host-check/boom")
    def boom():
        h.err(KeyError("nothing here"), 404)


async def main():
    missing = [m for m in MEMBERS if m not in set(dir(Host))]
    ck("Host lists every member of the contract", not missing, ", ".join(missing))
    from tares.daemon import make_app
    # the two people the keys below act as are on the cell before it starts: adding a second
    # person over HTTP needs a licence and is tested under ee/tests
    _people.seed(os.environ["TARES_DB"], [("Pat", "pat@example.com"), ("Ada", "ada@example.com")])
    app = make_app()
    store = app.state.store
    h = getattr(app.state, "host", None)
    ck("app.state.host is a Host", isinstance(h, Host))
    ck("the host carries the daemon's store and runtime",
       h is not None and h.store is store and h.runtime is app.state.runtime)
    add_route(h, app)

    async def post(path, token=None):
        hdr = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers=hdr) as cx:
            return await cx.post(path, json={})

    async with app.router.lifespan_context(app):
        keys = {}
        for who, scopes in (("pat", ["ingest"]), ("ada", ["admin"])):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                         headers={"Authorization": f"Bearer {TOKEN}"}) as cx:
                r = await cx.post("/api/keys", json={
                    "name": f"{who}-key", "scopes": scopes,
                    "person": {"email": f"{who}@example.com", "name": who.capitalize()}})
            keys[who] = r.json()["secret"]
        ck("both keys were made", len(keys) == 2 and all(keys.values()))

        r = await post("/ingest/host-check/who")
        ck("no key: 401 from the guard", r.status_code == 401, r.text)
        r = await post("/ingest/host-check/who", keys["pat"])
        ck("person key: 403 from require_admin", r.status_code == 403
           and r.json().get("detail") == "this needs an admin", f"{r.status_code} {r.text}")
        r = await post("/ingest/host-check/who", keys["ada"])
        body = r.json() if r.status_code == 200 else {}
        ck("admin key: 200", r.status_code == 200, f"{r.status_code} {r.text}")
        ck("admin key: current_person is the key's person", body.get("person") == "ada@example.com", body)
        ck("admin key: is_admin true, is_system false", body.get("admin") is True
           and body.get("system") is False, body)
        ck("caller() is the request being served", body.get("caller_is_request") is True, body)
        r = await post("/ingest/host-check/who", TOKEN)
        body = r.json() if r.status_code == 200 else {}
        ck("system credential: 200, no person, is_system",
           r.status_code == 200 and body.get("person") is None and body.get("system") is True,
           f"{r.status_code} {r.text}")
        r = await post("/ingest/host-check/boom", keys["ada"])
        ck("err raises the HTTP status it is given", r.status_code == 404
           and "nothing here" in r.text, f"{r.status_code} {r.text}")

        # the daemon's real helpers, called straight through the host
        ck("hidden_ids is a set", h.hidden_ids() == set())
        ck("projects() is the daemon's engine", hasattr(h.projects(), "list"))
        ck("resolve_project keeps an unknown ref", h.resolve_project("no-such-project") == "no-such-project")
        from types import SimpleNamespace
        ck("project_key is the request's project key",
           h.project_key(SimpleNamespace(state=SimpleNamespace(project_key="prj_1"))) == "prj_1")
        ck("project_key is None for any other credential",
           h.project_key(SimpleNamespace(state=SimpleNamespace())) is None)
        card = h.hidden_project({"id": "prj_1", "name": "Secret", "lead": "ada", "scope": "private",
                                 "goal": "do not leak", "tickets": [1]})
        ck("hidden_project names the project and its lead and nothing else",
           card == {"id": "prj_1", "name": "Secret", "scope": "private", "hidden": True,
                    "lead": "ada"}, card)
        ck("licence_state counts the people on the cell",
           h.licence_state()["used"] == len(store.list_people()) and h.licence_state()["used"] >= 3,
           h.licence_state())
        ck("licence_state is the cell's licence check: none here, the full shape",
           set(h.licence_state()) == STATE_KEYS and h.licence_state()["source"] == "none"
           and h.licence_state()["valid"] is False, h.licence_state())
        try:
            h.licence_required("Teams need a licence.")
            ck("licence_required raises", False, "no error")
        except LicenceRequired as e:
            ck("licence_required raises LicenceRequired with the message", e.message == "Teams need a licence.")

    # the class itself
    ck("every member is a method with a docstring",
       all(callable(getattr(Host, m)) and (getattr(Host, m).__doc__ or "").strip() for m in MEMBERS))
    ck("every member is type hinted", all(
        getattr(Host, m).__annotations__ for m in MEMBERS if m != "require_admin") and
       inspect.signature(Host.err).return_annotation is not inspect.Signature.empty)

    class FakeStore:
        def list_people(self):
            return [{"id": "a"}, {"id": "b"}]

    fake = Host(FakeStore(), "the-runtime")
    ck("Host(...) exposes store and runtime", fake.store.list_people()[0]["id"] == "a"
       and fake.runtime == "the-runtime")
    ck("licence_state is the daemon's helper, passed through",
       Host(FakeStore(), None, licence_state=lambda: {"used": 2}).licence_state() == {"used": 2})
    try:
        fake.licence_state()
        ck("licence_state without its helper is named", False, "no error")
    except RuntimeError as e:
        ck("licence_state without its helper is named", "licence_state" in str(e), str(e))
    try:
        Host(FakeStore(), None, licence_required=lambda m: (_ for _ in ()).throw(LicenceRequired(m))
             ).licence_required("no")
        ck("licence_required through the helper raises", False, "no error")
    except LicenceRequired as e:
        ck("licence_required through the helper raises", e.message == "no")
    try:
        Host(FakeStore(), None, licence_required=lambda m: None).licence_required("still")
        ck("licence_required never returns, even if the helper does", False, "returned")
    except LicenceRequired as e:
        ck("licence_required never returns, even if the helper does", e.message == "still")
    try:
        fake.is_admin(None)
        ck("a missing helper is named", False, "no error")
    except RuntimeError as e:
        ck("a missing helper is named", "is_admin" in str(e), str(e))
    try:
        Host(FakeStore(), None, nope=lambda: 1)
        ck("an unknown helper is refused", False, "no error")
    except TypeError as e:
        ck("an unknown helper is refused", "nope" in str(e), str(e))
    calls = []
    admin_no = Host(FakeStore(), None, is_admin=lambda r: False,
                    err=lambda e, code=400: calls.append((str(e), code)))
    try:
        admin_no.require_admin(object())
    except Exception:
        pass
    ck("require_admin asks err for a 403", calls == [("this needs an admin", 403)], calls)
    yes = Host(FakeStore(), None, is_admin=lambda r: True)
    ck("require_admin passes an admin", yes.require_admin(object()) is None)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
