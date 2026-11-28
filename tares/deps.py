"""What the routes need from the cell, as one object (TR-1814).

`make_app()` builds one `Deps` and hands it to every router under tares/routes/ and to every
module that adds routes of its own (factory_api, the screens, integrations_api, guardrails_api,
project_secrets, waiting_slack, claude_usage, and the ee team through `team.register_ee`). It
replaces the Host of TR-910: the same helpers, by the same names, now plain methods of one
dataclass instead of callables passed in by keyword.

The fields are the things the cell has: its store, its projects, the auth token it started with,
its database path, and the probes `/health` reads. `fx`, `morning` and `waiting_slack` are set by
`make_app()` once the factory's readers, the morning status and the Slack reader exist;
`team_reader` is set by `factory_api.register`.

The identity and visibility helpers are licence-independent by design (TR-915): never consult
licence or team in them.
"""
from __future__ import annotations

import contextvars
import hashlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, NoReturn

from fastapi import HTTPException, Request

from . import licence
from .envelope import now_utc
from .licence import LicenceRequired
from .store import ReadExclude

if TYPE_CHECKING:
    from .factory_projects import Projects
    from .store import Store


def _bearer(auth: str | None) -> str:
    return auth[7:].strip() if auth and auth.lower().startswith("bearer ") else ""


def _no_team_reader() -> SimpleNamespace:
    raise RuntimeError("the team reader is set by factory_api.register, which has not run yet")


@dataclass(eq=False)
class Deps:
    store: Store
    projects: Projects
    # the root credential (TARES_AUTH_TOKEN) the daemon started with; "" means auth is off
    auth_token: str = ""
    # the data directory, for the storage figures (the volume it sits on)
    data_dir: str = ""
    # how full the store is against its limit (daemon.storage_state for this store)
    storage: Callable[[], dict] = field(default=lambda: {})
    # what /health lists and says
    capabilities: list = field(default_factory=list)
    version: Callable[[], str | None] = field(default=lambda: None)
    started_mono: float = 0.0
    # the control plane's links /health hands a logged-out console, read when asked
    cloud_links: Callable[[], dict] = field(default=lambda: {})
    # set by make_app() as they are built
    fx: SimpleNamespace | None = None
    morning: Any = None
    waiting_slack: Any = None
    # the `c` the team interface takes (tares.team.Team); factory_api.register sets it
    team_reader: Callable[[], SimpleNamespace] = _no_team_reader
    # Who may see a private project (TR-625). The guard keeps the request being served here, so
    # the project helpers that ~70 routes call can check it without taking a request argument.
    # Auth off has no guard, so nothing is set and every project is visible.
    caller_var: contextvars.ContextVar = field(
        default_factory=lambda: contextvars.ContextVar("tares_caller", default=None))

    # ── errors and the request being served ──
    def err(self, e: Exception, code: int = 400) -> NoReturn:
        """Raise an HTTPException with status `code` and the text of `e`. Never returns."""
        raise HTTPException(status_code=code, detail=str(e))

    def caller(self) -> Request | None:
        """The request being served, or None when auth is off (the guard that records it only
        runs with auth on) or when called outside a request."""
        return self.caller_var.get()

    def auth_off(self) -> bool:
        """True when auth is off (no TARES_AUTH_TOKEN): the one person using the cell is `local`."""
        return not self.auth_token

    # ── who is calling ──
    def project_key(self, request: Request) -> str | None:
        """The project of the request's project key, or None for any other credential (and on an
        open instance, where no key is checked)."""
        return getattr(request.state, "project_key", None)

    # Licence-independent by design (TR-915): never consult licence or team here.
    def resolve_credential(self, request) -> tuple[set, dict] | tuple[None, None]:
        """Token from the request -> (scopes, identity), or (None, None) if unknown/absent."""
        store = self.store
        tok = _bearer(request.headers.get("authorization")) or request.headers.get("x-tares-token", "")
        if not tok:
            return None, None
        if self.auth_token and tok == self.auth_token:
            # the operator's credential, not a person
            return ({"read", "ingest", "admin"},
                    {"id": "env:auth", "name": "auth token (env)", "kind": "system", "person": None})
        key = store.find_api_key(hashlib.sha256(tok.encode()).hexdigest())
        if key:
            last = key.get("last_used_at")
            if last is None or (now_utc() - last).total_seconds() > 60:   # throttle write churn
                store.touch_api_key(key["id"])
            ident = {"id": f"key:{key['id']}", "name": key["name"],
                     "kind": "project" if key.get("project") else "key",
                     "person": self.key_person(key)}
            if key.get("project"):
                ident["project"] = key["project"]
            return set(key["scopes"]), ident
        return None, None

    @staticmethod
    def person_ref(p: dict | None) -> dict | None:
        """The part of a person a credential carries."""
        return ({"id": p["id"], "name": p["name"], "email": p["email"],
                 "picture": p.get("picture")} if p else None)

    # Licence-independent by design (TR-915): never consult licence or team here.
    def key_person(self, key: dict) -> dict | None:
        """Who a key acts as: the person it is bound to. A key nobody has bound acts as `local`
        while `local` is the only person on the cell (a single-user cell keeps working), and as
        nobody once a second person exists."""
        if key.get("person_id"):
            return self.person_ref(self.store.get_person(key["person_id"]))
        if self.store.only_local_person():
            return self.person_ref(self.store.get_person("local"))
        return None

    # Licence-independent by design (TR-915): never consult licence or team here.
    def current_person(self, request) -> dict | None:
        """The person making this request ({id, name, email}), or None for the system credential
        and for a key with no person. Always read from the credential, never from a header or the
        body. With auth off it is the person `local`."""
        ident = getattr(request.state, "credential", None)
        if ident is not None:
            return ident.get("person")   # resolved once, by the guard
        # auth off: look `local` up once per request and keep it on the request
        if self.auth_token:
            return None
        if not hasattr(request.state, "person"):
            request.state.person = self.person_ref(self.store.get_person("local"))
        return request.state.person

    def is_admin(self, request) -> bool:
        """The credential may manage people and keys: admin scope (the system credential has it),
        or auth off, where everyone is the admin."""
        if not self.auth_token:
            return True
        return "admin" in (getattr(request.state, "scopes", None) or [])

    def is_system(self, request) -> bool:
        """The system credential (the root TARES_AUTH_TOKEN, which is no person), or auth off,
        where the cell belongs to the one person using it."""
        if not self.auth_token:
            return True
        return (getattr(request.state, "credential", None) or {}).get("kind") == "system"

    def require_admin(self, request: Request) -> None:
        """Pass when the caller is an admin; otherwise raise a 403 with a plain message."""
        if not self.is_admin(request):
            self.err(PermissionError("this needs an admin"), 403)

    def by(self, request: Request, body: dict | None = None) -> str:
        """Who made a change: what the caller says (`by`), else the person behind its credential,
        else the caller's own default label (`by_default`, which the Claude Code tools send and
        which never beats a person), else the credential's own name."""
        said = str((body or {}).get("by") or "").strip()[:100]
        default = str((body or {}).get("by_default") or "").strip()[:100]
        cred = getattr(request.state, "credential", None) or {}
        person = self.current_person(request) or {}
        return said or person.get("name") or default or cred.get("name") or ""

    # ── what the caller may see ──
    # Licence-independent by design (TR-915): never consult licence or team here.
    def can_see(self, p: dict, request=None) -> bool:
        """May this caller see project `p`? A team project: anyone who gets this far. A private
        one: the system credential, a project key for it, and the people on it (its lead and its
        members). An admin who is none of those does not; they can only hand the lead over (see
        set_project_access). Rights come from the credential, never from people.is_admin (A9)."""
        request = request or self.caller_var.get()
        if request is None or p.get("scope") != "private":
            return True
        # A caller that knows no person can call as the system credential with this header:
        # every private project is then hidden from it. Only the system
        # credential can narrow itself this way; anyone else's header is ignored.
        if self.is_system(request) and request.headers.get("x-tares-viewer") != "public":
            return True
        if self.project_key(request) == p["id"]:
            return True
        person = self.current_person(request)
        return bool(person and self.store.is_project_member(p["id"], person["id"]))

    # Licence-independent by design (TR-915): never consult licence or team here.
    def hidden_projects(self) -> list[dict]:
        """The private projects this caller cannot see; usually none, and then one small query
        is all a cell-wide read adds (TR-629)."""
        return [p for p in self.store.private_projects() if not self.can_see(p)]

    # Licence-independent by design (TR-915): never consult licence or team here.
    def hidden_ids(self) -> set[str]:
        """The projects the caller cannot see: the private ones they are not on and, for every
        caller, the private ones that were deleted (TR-1697)."""
        private, deleted = self.store.private_and_deleted()
        return {p["id"] for p in private if not self.can_see(p)} | deleted

    # Licence-independent by design (TR-915): never consult licence or team here.
    def read_exclude(self) -> dict | None:
        """What event reads leave out: the rows of the projects the caller cannot see, the
        deleted private ones among them for every caller."""
        private, deleted, records = self.store.private_and_records()
        hp = [p for p in private if not self.can_see(p)]
        if not hp and not deleted:
            return None
        return ReadExclude(
            {"ids": [p["id"] for p in hp] + sorted(deleted), "names": [p["name"] for p in hp],
             "shared": []}, records)

    # Licence-independent by design (TR-915): never consult licence or team here.
    def owned_by_hidden(self, kind: str, name: str, hidden: set[str]) -> bool:
        """An object whose every project is hidden from the caller (one it shares with a project
        they can see stays visible)."""
        if not hidden:
            return False
        uids = self.store.projects_using(kind, name)
        return bool(uids) and set(uids) <= hidden

    # Licence-independent by design (TR-915): never consult licence or team here.
    def hidden_project(self, p: dict) -> dict:
        """A private project as someone not on it sees it: enough to name it and to hand its
        lead over, nothing of what is in it."""
        return {"id": p["id"], "name": p["name"], "scope": "private", "hidden": True,
                "lead": p["lead"]}

    # ── projects, tickets and shared ids ──
    def project_or_404(self, uid: str) -> dict:
        """The project with id `uid`. Raises a 404 when there is none or it is private and the
        caller is not on it."""
        p = self.store.get_project(uid)
        if p is None or not self.can_see(p):   # a private one the caller cannot see is unknown
            self.err(KeyError(f"unknown project {uid!r}"), 404)
        return p

    def resolve_project(self, ref: str) -> str:
        """A project's id from its id or name (the ref itself when there is none: a 404 later)."""
        p = self.store.get_project(ref) or self.store.get_project_by_name(ref)
        return p["id"] if p else ref

    def cc_source(self) -> str:
        """The source session lines are stored under: always `claude_code` (TR-1804)."""
        from .session_map import SOURCE as SESSION_SOURCE
        return SESSION_SOURCE

    # The cell's global docs (its AGENTS.md and memory): made the first time a project has docs,
    # included in every project that has docs, so every session working on a project reads them.
    def global_ids(self, create: bool = False) -> dict[str, str]:
        """{kind: doc id} of the global docs that exist; with `create`, make the missing ones."""
        from .integrations.builtin import memory_tares
        return memory_tares.global_ids(self.store, create)

    def ensure_globals(self, uid: str) -> dict[str, str]:
        """A project with docs includes the global docs (made now if this is the first one)."""
        mine = self.store.list_docs(uid)
        if not mine:
            return self.global_ids()
        ids = self.global_ids(create=True)
        have = {d["id"] for d in mine}
        for doc_id in ids.values():
            if doc_id not in have:
                self.store.use_doc(uid, doc_id)
        return ids

    def ticket_out(self, uid: str, t: dict, **kw) -> dict:
        """One ticket of project `uid` in the shape the API returns (tares/routes/tickets.py)."""
        from .routes import tickets
        return tickets.ticket_out(self, uid, t, **kw)

    def tickets_out(self, uid: str) -> list[dict]:
        """Every ticket of project `uid` in the shape the API returns."""
        from .routes import tickets
        return tickets.tickets_out(self, uid)

    def area_owner(self, repo: str) -> dict | None:
        """The person who owns the GitHub repo `owner/name` (any case), or None."""
        from . import team as team_mod
        return team_mod.get().area_owner(self.store, repo)

    # ── model usage: one ledger row per model call the cell makes ──
    def record_model_usage(self, model: str, usage: dict, key_source: str = "",
                           kind: str = "anthropic", provider_id: str | None = None) -> None:
        """One ledger row per summary (the standup, a milestone, the report), so the
        cell's spend meter covers everything that talks to a model. `key_source` is the origin of
        the key that PAID for it, captured by the caller when it resolved the key, never
        re-resolved here: the stored key could have changed meanwhile. The surface stays "ask",
        the name these calls had in the ledger before agents went (TR-1810)."""
        from .pricing import price_usage
        self.store.record_model_usage(
            "ask", "", "", model, usage["calls"],
            usage["input_tokens"], usage["output_tokens"],
            usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"],
            price_usage(kind, model, usage),
            key_source=key_source or None, provider=provider_id)

    # ── the licence ──
    def licence_state(self) -> dict:
        """The cell's licence as `tares.licence.state` says it (valid, source, org, seats, used,
        features, expires, days_left, expired, over_seats, ee_allowed, reason), plus `stored` and
        `env_set`. Checked at most once a minute; `used` is the number of people on the cell now."""
        return licence.current(self.store)

    def licence_required(self, message: str) -> NoReturn:
        """Stop an action that needs a licence the cell does not have: the caller gets HTTP 402
        with {code: "licence_required", message, url}. `message` is plain words for the person.
        Never returns."""
        licence.licence_required(message)
        raise LicenceRequired(message)   # licence_required always raises; this keeps the promise
