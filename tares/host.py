"""The daemon's route helpers, as one importable class (TR-910).

Code that lives outside tares/daemon.py (the screens, the factory modules, later ee/) needs a few
things the daemon already knows how to do: who is calling, whether they are an admin, which
projects they may see, how to turn an error into an HTTP answer. Those helpers are closures inside
`make_app()`. `make_app()` builds one `Host` from them and hands it on (`app.state.host`, and the
`h` argument of `factory_api.register` and `screens.register_all`).

A `Host` holds no logic of its own, apart from `require_admin`. Each method
calls the helper the daemon gave it, so a call through `h` does exactly what the daemon's own
route would do. A helper the daemon did not give raises a RuntimeError that names it.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, NoReturn

from fastapi import Request

from .licence import LicenceRequired

if TYPE_CHECKING:
    from .projects import Engine as ProjectEngine

# the helpers make_app() passes in; each is a method of Host with the same name
HELPERS = (
    "err", "caller", "current_person", "is_admin", "is_system", "project_key", "start_who", "by",
    "can_see", "hidden_ids", "read_exclude", "hidden_sources", "project_or_404", "resolve_project",
    "cc_source", "global_ids", "ensure_globals", "ticket_out", "tickets_out", "area_owner",
    "hidden_project", "projects", "licence_state", "licence_required",
)


class Host:
    """What a screen or a module needs from the daemon. Built once, by `make_app()`.

    `store` is the Store, `runtime` the Runtime. The rest are helper callables passed by keyword
    (see HELPERS); a name that is not a helper raises TypeError, so a typo cannot go unnoticed."""

    def __init__(self, store: Any, runtime: Any, **helpers: Callable):
        unknown = sorted(set(helpers) - set(HELPERS))
        if unknown:
            raise TypeError(f"Host has no helper named {', '.join(unknown)}")
        self.store = store
        self.runtime = runtime
        self._helpers = helpers

    def _call(self, name: str, *args, **kw):
        fn = self._helpers.get(name)
        if fn is None:
            raise RuntimeError(f"this Host was built without the {name!r} helper")
        return fn(*args, **kw)

    # ── errors and the request being served ──
    def err(self, e: Exception, code: int = 400) -> NoReturn:
        """Raise an HTTPException with status `code` and the text of `e`. Never returns."""
        self._call("err", e, code)
        raise e   # the daemon's helper always raises; this keeps the NoReturn promise

    def caller(self) -> Request | None:
        """The request being served, or None when auth is off (the guard that records it only
        runs with auth on) or when called outside a request."""
        return self._call("caller")

    # ── who is calling ──
    def current_person(self, request: Request) -> dict | None:
        """The person behind the request's credential ({id, name, email, picture}), or None for
        the system credential and for a key with no person. Auth off: the person `local`."""
        return self._call("current_person", request)

    def is_admin(self, request: Request) -> bool:
        """True when the credential has the admin scope (the system credential does) or auth is
        off, where everyone is the admin."""
        return self._call("is_admin", request)

    def is_system(self, request: Request) -> bool:
        """True for the root TARES_AUTH_TOKEN credential, which is no person, and when auth is
        off, where the cell belongs to the one person using it."""
        return self._call("is_system", request)

    def project_key(self, request: Request) -> str | None:
        """The id of the project the request's key belongs to, or None for any other credential."""
        return self._call("project_key", request)

    def start_who(self, request: Request) -> str:
        """The key of whose Start checklist hide applies to this request."""
        return self._call("start_who", request)

    def by(self, request: Request, body: dict | None = None) -> str:
        """Who made a change, as a name: the `by` field of `body` when it has one, else the
        caller's person, else `body`'s `by_default`, else the credential's own name ("" if none)."""
        return self._call("by", request, body)

    def require_admin(self, request: Request) -> None:
        """Pass when the caller is an admin; otherwise raise a 403 with a plain message."""
        if not self.is_admin(request):
            self.err(PermissionError("this needs an admin"), 403)

    # ── what the caller may see ──
    def can_see(self, p: dict) -> bool:
        """True when the caller may see project `p`: a team project, or a private one they are on."""
        return self._call("can_see", p)

    def hidden_ids(self) -> set[str]:
        """The ids of the private projects the caller cannot see; empty for most callers."""
        return self._call("hidden_ids")

    def read_exclude(self) -> dict | None:
        """What an event read must leave out for this caller ({ids, names, shared}), or None
        when no project is hidden from them."""
        return self._call("read_exclude")

    def hidden_sources(self) -> set[str]:
        """The names of the sources that belong only to projects hidden from the caller."""
        return self._call("hidden_sources")

    def hidden_project(self, p: dict) -> dict:
        """A private project as someone not on it sees it: {id, name, scope, hidden, lead} and
        nothing of what is in it."""
        return self._call("hidden_project", p)

    # ── projects, tickets and shared ids ──
    def projects(self) -> ProjectEngine:
        """The daemon's ProjectEngine, which starts and wires projects."""
        return self._call("projects")

    def project_or_404(self, uid: str) -> dict:
        """The project with id `uid`. Raises a 404 when there is none or it is private and the
        caller is not on it."""
        return self._call("project_or_404", uid)

    def resolve_project(self, ref: str) -> str:
        """A project's id from its id or its name; `ref` itself when no project matches."""
        return self._call("resolve_project", ref)

    def cc_source(self) -> str:
        """The name of the cell's claude_code source (`claude_code` when it has none)."""
        return self._call("cc_source")

    def global_ids(self, create: bool = False) -> dict[str, str]:
        """{kind: doc id} of the cell's global docs (its AGENTS.md and memory) that exist; with
        `create`, the missing ones are made first."""
        return self._call("global_ids", create)

    def ensure_globals(self, uid: str) -> dict[str, str]:
        """Attach the global docs to project `uid` when it has docs; returns {kind: doc id}."""
        return self._call("ensure_globals", uid)

    def ticket_out(self, uid: str, t: dict, **kw) -> dict:
        """One ticket of project `uid` in the shape the API returns."""
        return self._call("ticket_out", uid, t, **kw)

    def tickets_out(self, uid: str) -> list[dict]:
        """Every ticket of project `uid` in the shape the API returns."""
        return self._call("tickets_out", uid)

    def area_owner(self, repo: str) -> dict | None:
        """The person who owns the GitHub repo `owner/name` (any case), or None."""
        return self._call("area_owner", repo)

    # ── the licence ──
    def licence_state(self) -> dict:
        """The cell's licence as `tares.licence.state` says it (valid, source, org, seats, used,
        features, expires, days_left, expired, over_seats, ee_allowed, reason), plus `stored` and
        `env_set`. Checked at most once a minute; `used` is the number of people on the cell now."""
        return self._call("licence_state")

    def licence_required(self, message: str) -> NoReturn:
        """Stop an action that needs a licence the cell does not have: the caller gets HTTP 402
        with {code: "licence_required", message, url}. `message` is plain words for the person.
        Never returns."""
        self._call("licence_required", message)
        raise LicenceRequired(message)   # the daemon's helper always raises; this keeps the promise

    # ── the team interface ──
    def team_reader(self) -> SimpleNamespace:
        """The `c` the team interface (`tares.team.Team`) takes as its first argument: what the
        factory modules know, as one namespace (store, host, stations, work_of, work_person,
        my_projects, queue, settings, desk_is_mine, is_mine, leads, stored, crews_of, mirror_in,
        mirror_out, visible_grant, grants_view). `factory_api.register` sets it; before that it raises
        a RuntimeError."""
        raise RuntimeError("this Host was built without the 'team_reader' helper")
