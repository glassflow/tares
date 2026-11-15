"""The team interface (TR-914): everything a cell does only when it has more than one person.

Core knows one person. `SingleTeam` is that cell: nobody to assign to, no areas, no consent, one
crew per name. The enterprise code in `tares_ee` plays the same interface as `tares_ee.team.Team`,
a subclass of `SingleTeam`, and `get()` hands it out only while the licence allows it.

    from tares import team as team_mod
    team_mod.get().resolve_crew(name, person_id, machine)

(`tares_ee/team_home.py` has a class `HomeRead` of its own, one person's home read. It is not part of
this interface.)

Three rules shape the interface:

- A method that has nothing to do on a single-person cell does nothing and says so in its return
  value. A method that cannot answer without a team raises `licence.LicenceRequired`.
- Who a key belongs to never goes through `Team`: `_resolve_credential`, `_key_person`,
  `_current_person` and `store.key_person_id` stay in core. An existing person's key and sign-in
  resolve to that person in every licence state. Only making a NEW person row asks
  `can_add_person`; the system credential's `owner: true` claim of an unclaimed `local` is always
  allowed.
- `c` in the grant, hand-over and team-home methods is the namespace `factory_api.register` builds in
  `team_reader()`. The move tickets add the fields they need to it.

Route order in `make_app` (see the comment there): core routes, `factory_api.register`,
`screens.register_all`, then `Team.register_routes` of the ee team (whatever the licence; each ee
route answers `licence_required` itself), then `register_fallbacks`, then the console catch-all.
"""
from __future__ import annotations

import importlib.metadata
import logging
import threading
from typing import Any, Callable

from . import factory_owners as owners_mod
from . import licence

log = logging.getLogger("tares.team")

ENTRY_GROUP = "tares.ee"
ENTRY_NAME = "team"

# Held around "can this team add a person" and the insert that follows, so two requests cannot both
# pass the seat check and write past the seats. The daemon handlers run in a thread pool.
ADD_LOCK = threading.Lock()

ADD_PERSON_REASON = "Adding people needs an enterprise licence. Tares works for one person without one."
ASSIGN_REASON = "Assigning tickets to people needs an enterprise licence."
ADDRESS_REASON = "Addressing a question to someone else needs an enterprise licence."


class Team:
    """The contract. Every method below is overridden by `SingleTeam`; the enterprise team
    overrides the ones that mean something with more than one person. A later ticket that adds a
    method adds it here, to `SingleTeam`, and to `METHODS` in tests/test_team_interface.py."""

    # ── people ──
    def can_add_person(self, store) -> tuple[bool, str | None]:
        """(True, None) when the cell may make one more person row, else (False, reason in plain
        words). The caller turns False into `host.licence_required(reason)`. Called only before a
        NEW row: finding an existing person (by id or email) and the system credential's
        `owner: true` claim of an unclaimed `local` never ask."""
        raise NotImplementedError

    # ── areas ──
    def areas(self, store) -> list[dict]:
        """The areas of the cell in the shape of `GET /api/areas`. One area is one GitHub repo
        (`owner/name`, no path) and the person who owns it."""
        raise NotImplementedError

    def area_owner(self, store, repo: str) -> dict | None:
        """The store person dict that owns the repo `owner/name` (any case), or None."""
        raise NotImplementedError

    # ── ticket assignees ──
    def assignee_from_linear(self, store, assignee, cache=None) -> tuple[str | None, str | None] | None:
        """(person id, raw assignee text) for a Linear assignee, or None, which means "assignees are
        not synced: leave the stored columns as they are". None is not "no assignee"."""
        raise NotImplementedError

    def assignee_from_github(self, store, issue) -> tuple[str | None, str | None] | None:
        """The same for a GitHub issue. None means "not synced"."""
        raise NotImplementedError

    def assignee_warn_nameless(self, project, count) -> None:
        """Tell the project that `count` synced tickets have an assignee nobody on the cell matches.
        Returns nothing."""
        raise NotImplementedError

    def assignee_sync_join(self, store, project, person_id, status) -> None:
        """A synced assignee joins the project's members at `status`. Returns nothing."""
        raise NotImplementedError

    def assignee_join(self, store, project, person_id, added_by=None) -> None:
        """Assigning a ticket joins its assignee to the project. Returns nothing."""
        raise NotImplementedError

    def assignee_resolve(self, store, ref) -> tuple[str | None, str | None]:
        """(person id, raw text) for a person the caller names (id, email or name). An empty ref
        gives (None, None). Raises `licence.LicenceRequired` when the cell cannot assign."""
        raise NotImplementedError

    # ── grants: consent only (the grant kinds stay in core) ──
    def grant_waiting_for(self, c, grant: dict) -> list[dict]:
        """The people who must still agree before `grant` takes effect, as store person dicts."""
        raise NotImplementedError

    def grants_waiting_for_me(self, c, grants: list[dict], request) -> list[dict]:
        """The grants in `grants` that wait for the caller of `request` to agree."""
        raise NotImplementedError

    def settle_grant(self, c, grant: dict, request) -> dict:
        """`grant` after its consent is settled for this request: unchanged when nobody has yet
        agreed, so a grant that waits never takes effect without the team."""
        raise NotImplementedError

    # ── hand-over and spec questions ──
    def handover_blockers(self, c, project: dict) -> list[str]:
        """Plain-word reasons the project cannot be handed over yet (a missing sign-off). Empty
        means nothing blocks."""
        raise NotImplementedError

    def open_questions_for(self, c, person_id: str, hidden, want) -> list[dict]:
        """The spec questions open for `person_id`. `hidden` is the set of project ids the caller
        may not see and `want` the filter the caller asked for."""
        raise NotImplementedError

    # ── crews ──
    def resolve_crew(self, name: str | None, person_id: str | None, machine: str | None) -> tuple[str, str]:
        """The (name, machine) the caller then gives to `store.resolve_crew(name, person_id,
        machine, at)`, which returns the crew's id."""
        raise NotImplementedError

    # ── team home (the team home itself is ee code, the three row filters are core) ──
    def team_home(self, c, request, now) -> dict:
        """The team page's body for the caller, or {} when the cell has no team page."""
        raise NotImplementedError

    def project_waiting(self, c, request, now, project: str) -> dict | None:
        """What waits in one project, by the rule of `team_home`: {mine, others}. `mine` is the
        caller's items of the project, exactly as their home lists them; `others` is what the same
        rule gives each other person there, named for them, with nothing to press. None when the
        cell has no team page (the single-person list then answers)."""
        raise NotImplementedError

    def own_stations(self, c, request, rows: list) -> list:
        """`rows` (the crew's stations) cut to what the caller may see. A one-person cell sees all
        of them; on a cell with more than one person a person caller sees only their own. The
        single-person implementation does this without a licence, so a team database that is not
        licensed never shows one person's rows to another."""
        raise NotImplementedError

    def own_desk(self, c, request, rows: list) -> list:
        """`rows` (desk items) cut to what the caller may see. Same rule as `own_stations`."""
        raise NotImplementedError

    def own_tickets(self, c, request, tickets, projects) -> Any:
        """`tickets` cut to what the caller may see, in the shape the caller passed in. Same rule
        as `own_stations`."""
        raise NotImplementedError

    def team_projects(self, c, request, open_ps, tickets, rows, which, now, waiting) -> dict:
        """The team's view of its projects for the caller, or {} when the cell has no team view."""
        raise NotImplementedError

    # ── registration ──
    def register_routes(self, app, host) -> None:
        """Add the team's HTTP routes to `app`. `host` is `app.state.host`. Every route starts with
        `if not licence.ee_allowed(): host.licence_required("...")`."""
        raise NotImplementedError

    def mcp_tools(self) -> list[Callable]:
        """The MCP tools the team adds (TR-1041 registers them). Hidden while the licence does not
        allow them."""
        raise NotImplementedError


class SingleTeam(Team):
    """One person on the cell. The default whenever there is no licence or no ee code."""

    def can_add_person(self, store):
        return False, ADD_PERSON_REASON

    def areas(self, store):
        return []

    def area_owner(self, store, repo):
        return None

    def assignee_from_linear(self, store, assignee, cache=None):
        return None

    def assignee_from_github(self, store, issue):
        return None

    def assignee_warn_nameless(self, project, count):
        return None

    def assignee_sync_join(self, store, project, person_id, status):
        return None

    def assignee_join(self, store, project, person_id, added_by=None):
        return None

    def assignee_resolve(self, store, ref):
        if not ref:
            return None, None
        raise licence.LicenceRequired(ASSIGN_REASON)

    def grant_waiting_for(self, c, grant):
        return []

    def grants_waiting_for_me(self, c, grants, request):
        return []

    def settle_grant(self, c, grant, request):
        return grant

    def handover_blockers(self, c, project):
        return []

    def open_questions_for(self, c, person_id, hidden, want):
        return []

    def resolve_crew(self, name, person_id, machine):
        return (name or "").strip() or "crew", ""

    def team_home(self, c, request, now):
        return {}

    def project_waiting(self, c, request, now, project):
        return None

    @staticmethod
    def _applies(c, request) -> bool:
        """True when rows are cut to the caller's own: a cell with more than one person and a
        caller with a person (or the system). Licence and team are not consulted."""
        if c.store.only_local_person():
            return False
        return not (c.h.current_person(request) is None and not c.h.is_system(request))

    def own_stations(self, c, request, rows):
        if not self._applies(c, request):
            return rows
        me = c.work_person(request)["id"]
        return [s for s in rows if (s["person"] or {}).get("id") == me]

    def own_desk(self, c, request, rows):
        if not self._applies(c, request):
            return rows
        me = c.work_person(request)["id"]
        o = owners_mod.Owners(c.store)   # one read of the people for every row
        return [d for d in rows if c.desk_is_mine(d, me, o)]

    def own_tickets(self, c, request, tickets, projects):
        if not self._applies(c, request):
            return tickets
        me = c.work_person(request)["id"]
        by_id = {p["id"]: p for p in projects}
        o = owners_mod.Owners(c.store)
        return {pid: [t for t in ts if c.is_mine(t, me, by_id.get(pid), o)]
                for pid, ts in tickets.items()}

    def team_projects(self, c, request, open_ps, tickets, rows, which, now, waiting):
        return {}

    def register_routes(self, app, host):
        return None

    def mcp_tools(self):
        return []


# ── the loader ──
_SINGLE = SingleTeam()
_UNSET: Any = object()
_ee_team: Any = _UNSET


def _load_ee() -> Team | None:
    try:
        found = importlib.metadata.entry_points(group=ENTRY_GROUP, name=ENTRY_NAME)
        for ep in found:
            built = ep.load()()
            if not isinstance(built, Team):   # a wrong type would fail later, on a request
                log.warning("tares.team: ee team did not load: %s is not a tares.team.Team",
                            type(built).__name__)
                return None
            return built
        return None
    except Exception as e:   # a broken ee package must not stop the cell
        log.warning("tares.team: ee team did not load: %s: %s", type(e).__name__, e)
        return None


def _ee() -> Team | None:
    """The ee team, loaded once per process through the `tares.ee` entry point `team`, or None
    when there is none or it did not load (one log line says why). Never raises."""
    global _ee_team
    if _ee_team is _UNSET:
        _ee_team = _load_ee()
    return _ee_team


def get() -> Team:
    """The team to use now: the ee team while the licence allows it and it loaded, else the shared
    `SingleTeam`. It reads the last licence check (`licence.ee_allowed`) and never runs one: the
    watch in `licence.run` refreshes the check every minute, and a PUT or DELETE of the key
    refreshes it at once, so a key saved or removed applies without a restart. An expired licence
    still gives the ee team; its `can_add_person` refuses new people."""
    ee = _ee()
    return ee if ee is not None and licence.ee_allowed() else _SINGLE


def enabled() -> bool:
    """True while the ee team is the one `get()` hands out (it loaded and the licence allows it)."""
    return get() is not _SINGLE


def register_ee(app, host) -> None:
    """Add the ee team's routes to `app`. If registering raises, the cell runs as a single-person
    cell: the routes it added are taken out again, the ee team is dropped (so `get()` answers
    `SingleTeam` whatever the licence), and one log line says why. Never raises (TR-1057)."""
    global _ee_team
    ee = _ee()
    if ee is None:
        return
    before = len(app.router.routes)
    try:
        ee.register_routes(app, host)
    except Exception as e:
        log.warning("tares.team: ee routes did not register, running without ee: %s: %s",
                    type(e).__name__, e)
        del app.router.routes[before:]
        _ee_team = None


def _set_ee_for_tests(team: Team | None) -> None:
    """Put a fake ee team in place (None: no ee team) without an installed entry point."""
    global _ee_team
    _ee_team = team


def _reset_ee_for_tests() -> None:
    """Forget the loaded ee team so the next `_ee()` looks at the entry points again."""
    global _ee_team
    _ee_team = _UNSET


# ── routes that moved out of core ──
# (method, path, message): one row per route that lives in ee. Without ee the row answers
# `licence_required`. A ticket that moves a route out of core adds its rows in the same PR.
AREAS_REASON = "Repo owners need an enterprise licence."
ACCESS_REASON = "Changing a project's lead, privacy or members needs an enterprise licence."
CONSENT_REASON = "Agreeing to someone else's grant needs an enterprise licence."
QUESTIONS_REASON = "Questions to repo owners need an enterprise licence."
FALLBACKS: list[tuple[str, str, str]] = [
    ("GET", "/api/areas", AREAS_REASON),
    ("PUT", "/api/areas", AREAS_REASON),
    ("PUT", "/api/areas/{owner}/{name}", AREAS_REASON),
    ("DELETE", "/api/areas/{owner}/{name}", AREAS_REASON),
    ("PATCH", "/api/projects/{uid}", ACCESS_REASON),
    ("POST", "/api/projects/{uid}/members", ACCESS_REASON),
    ("DELETE", "/api/projects/{uid}/members/{person_id}", ACCESS_REASON),
    ("POST", "/api/grants/{gid}/consent", CONSENT_REASON),
    ("POST", "/api/grants/{gid}/decline", CONSENT_REASON),
    ("POST", "/api/projects/{uid}/questions", QUESTIONS_REASON),
    ("GET", "/api/projects/{uid}/questions", QUESTIONS_REASON),
    ("POST", "/api/projects/{uid}/questions/{qid}/answer", QUESTIONS_REASON),
]


def register_fallbacks(app, host) -> None:
    """Add one route per row of `FALLBACKS` that answers `host.licence_required(message)`."""
    def answer(message: str) -> Callable:
        def handler():   # no parameters: FastAPI would read a default as a query value
            host.licence_required(message)
        return handler

    for method, path, message in list(FALLBACKS):
        app.add_api_route(path, answer(message), methods=[method], include_in_schema=False)
