"""The auth guard: which scope each request needs, what a project key may do, and the middleware
that checks the credential (TR-1814: moved out of make_app()).

Three scopes: read (consume), ingest (contribute: /ingest, the crew's own writes), admin
(configure: credentials, keys, settings). Credentials: the env TARES_AUTH_TOKEN is the implicit
root (admin, non-revocable), plus revocable scoped keys in the api_keys table
(docs/design/api-keys.md). Auth off (no token): no middleware, the instance is fully open.
"""
from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .deps import Deps
from .routes.slack import SLACK_EVENTS_PATH


def _is_ingest(path: str) -> bool:
    return path.startswith("/ingest/")


# Where GitHub sends the browser back during "Create GitHub App" and after an install. A redirect
# carries no Tares token, so these two GETs are public to the auth middleware. The creation callback
# needs our signed `state`; the install callback needs the state, or an installation on the App
# owner's account (see github_app_installed).
GITHUB_APP_CALLBACKS = ("/api/integrations/github/apps/callback",
                        "/api/integrations/github/apps/installed")


def _public(method: str, path: str) -> bool:
    """Reachable without the auth token: CORS preflight, the health probe, ingest (own token), the
    Slack inbound endpoint (own signature), and the console SPA shell + static assets (any GET that
    isn't an API/data route)."""
    if method == "OPTIONS" or path in ("/health", "/metrics"):
        # /metrics like /health: a scraper inside the cluster carries no token, and the body holds
        # counts only, never an entity or a payload
        return True
    if _is_ingest(path):
        return True
    if method == "POST" and path == SLACK_EVENTS_PATH:
        # Slack calls this from its own infrastructure and cannot carry our bearer token, so it has
        # to be public to THIS middleware — but it is not unauthenticated. It is gated by an
        # HMAC-SHA256 signature over the raw body plus a 5-minute replay window (slack_verify.py),
        # and returns 503 rather than serving anything when no signing secret is configured.
        # Exactly one method on exactly one path: no prefix match, so nothing else rides in on it.
        return True
    if method == "GET" and path in GITHUB_APP_CALLBACKS:
        return True      # gated by the signed state, see GITHUB_APP_CALLBACKS
    return method in ("GET", "HEAD") and not path.startswith("/api/")


def install(app: FastAPI, deps: Deps) -> None:
    """Add the guard to `app` when auth is on. Called by make_app() before the timing middleware,
    so the timing stays the outermost one."""
    store = deps.store

    # ── auth: scoped credentials ──────────────────────────────────────────────
    # Three scopes: read (consume), ingest (contribute: /ingest, the crew's own writes), admin
    # (configure: credentials, keys, settings).
    # Credentials: the env AUTH_TOKEN is the implicit root (admin, non-revocable), plus revocable
    # scoped keys in the api_keys table (docs/design/api-keys.md).
    # the only writes a read key may make: reads that take a body (none today). Every other
    # write, including a route added later, needs admin.
    _READ_WRITES: tuple[str, ...] = ()
    _PERSONAL_LINES = re.compile(r"^/api/docs/personal/[^/]+/lines$")

    # the route lives in ee (tares_ee/areas.py); the rule stays here, or a person's own claim would need admin
    _AREA_ONE = re.compile(r"^/api/areas/[^/]+/[^/]+$")
    _GRANT_WRITE = re.compile(r"^/api/grants(/[^/]+/(consent|revoke|decline))?$")
    _REPLY_WRITE = re.compile(r"^/api/desk/[^/]+/reply$")
    _PROJECT_INTEGRATION = re.compile(
        r"^/api/projects/[^/]+/(integrations/[^/]+(/test)?|required-checks|guardrails/pairs(/[^/]+)?)$")
    _ANSWER_WRITE = re.compile(r"^/api/projects/[^/]+/questions/[^/]+/answer$")
    _SELF_JOIN = re.compile(r"^/api/projects/[^/]+/join$")
    _BRAIN_DECISION_WRITE = re.compile(r"^/api/brain/decisions(/[^/]+)?$")

    def _webhook_key(key: str) -> bool:
        """True when `key` is a GitHub App's webhook key. A store that cannot answer says no: the
        delivery then needs a Tares credential, and other ingest is not held up."""
        if key in ("claude_code", "codex"):
            return False   # the runtime intakes, the busiest ingest paths: never a webhook key
        try:
            return store.integration_key(key) is not None
        except Exception:   # noqa: BLE001
            return False

    def _required_scope(method: str, path: str) -> str | None:
        """None = public. 'any' = any valid credential. Reads of credentials and every write are
        admin, except the few in _READ_WRITES: a read with a body."""
        if (method == "POST" and path.startswith("/ingest/")
                and _webhook_key(path[len("/ingest/"):])):
            # a GitHub App's webhook is authenticated by the signature the route verifies on the
            # raw body; GitHub cannot send a Tares key
            return None
        if method == "POST" and _is_ingest(path):
            return "ingest"   # before _public(): ingest paths are "public" only in the sense of
                              # not needing the auth token — they have their own scope
        if _public(method, path):
            return None
        if path == "/api/me/slack":
            return "any"   # the caller's own Slack messages (TR-1458): the handler needs a person
        if path == "/api/whoami" or path == "/api/home/start/hide":
            return "any"   # the second one only changes what the caller's own home shows
        if method == "PUT" and _AREA_ONE.match(path):
            return "ingest"   # an admin sets any area; a person may claim an unowned repo for
                              # themselves (TR-627): the handler decides, it needs the credential
        if method == "POST" and _GRANT_WRITE.match(path):
            return "ingest"   # a lead, an area owner or a person for themselves (TR-630): the
                              # handler decides by kind, it needs the credential's person
        if method == "POST" and (_ANSWER_WRITE.match(path) or _REPLY_WRITE.match(path)):
            return "ingest"   # an area owner answering a question addressed to them (TR-636), or
                              # a person answering their own desk item from the console (TR-740):
                              # the handler decides by the credential's person
        if method in ("POST", "DELETE") and _BRAIN_DECISION_WRITE.match(path):
            return "ingest"   # anyone on the cell adds a decision by hand, and takes back their own
                              # (TR-1212): the enterprise handler decides who may delete, from the
                              # credential's person. A read key is refused here
        if method == "POST" and _SELF_JOIN.match(path):
            return "ingest"   # a person putting themself on a team project (TR-1720): the enterprise
                              # handler decides, from the credential's person. A read key is refused
        if method != "GET" and _PROJECT_INTEGRATION.match(path):
            return "ingest"   # a project's own integration choice and its test (TR-842), and
                              # its required checks (TR-854) and its guardrail pairs (TR-863): an admin or the project's lead;
                              # the handler decides, it needs the credential's person. A read
                              # key is refused here
        if method == "PATCH" and path.startswith("/api/people/"):
            return "read"   # an admin, or a key bound to that person changing their own name:
                            # the handler decides, it needs the credential's person
        if path == "/api/factory/github/token":
            return "ingest"  # a GitHub token for the crew: a GET, but it hands out a credential, so
                             # a read key is refused; the handler needs the credential's person
        if method == "POST" and path == "/api/crew/machine":
            return "ingest"  # the command line's report after `tares login` (TR-1312): a name and a
                             # version, from a key that can write the crew's settings
        if method == "PUT" and path == "/api/crew/codex-limits":
            return "ingest"  # a crew's own reading of its Codex account's limits (TR-1597), as its
                             # Claude plan reading comes in over /ingest/claude_code. A read key is refused
        if (method == "PUT" and path == "/api/crew/settings") or (
                method == "POST" and _PERSONAL_LINES.match(path)):
            return "ingest"  # a person's own crew settings and personal docs (TR-624): the
                             # handler decides whose, from the credential's person. A read key
                             # is refused here; admin implies ingest
        # /api/settings holds instance credentials (the Anthropic key): admin even to READ, since
        # a read tells you whether and where a credential is configured.
        if path in ("/api/factory/settings", "/api/factory/setup"):
            return "admin"   # the factory settings page and the factory setup screen (TR-871): the
                             # keys, the crew's limits, the Slack channels, the connected
                             # integrations. A read key is refused, as on the other admin-only pages
        if (path.startswith("/api/keys") or path.startswith("/api/settings")
                or path.startswith("/api/linear")):
            return "admin"
        m = _PROJECT_PATH.match(path)
        if m and (m.group(2) or "").startswith("/keys"):
            return "admin"   # a project's keys: credentials
        if method not in ("GET", "HEAD") and path not in _READ_WRITES:
            return "admin"
        return "read"

    # ── project keys (TR-335): what a key that belongs to one project may do ──
    # An allowlist, not a denylist: a route that is not named here answers 403 for a project key,
    # so a route added later is closed to project keys until someone decides otherwise. Each entry
    # is the scope the key needs; the handlers narrow what the allowed routes return to the key's
    # project (request.state.project_key).
    _PROJECT_PATH = re.compile(r"^/api/projects/([^/]+)(/.*)?$")
    _PK_ROUTES = {("GET", "/api/whoami"): "any", ("GET", "/api/projects"): "read"}
    _PK_PROJECT_ROUTES = [   # (method, the path after /api/projects/<its id>, scope)
        ("GET", re.compile(r"^$"), "read"),
        ("GET", re.compile(r"^/timeline$"), "read"),
        ("GET", re.compile(r"^/docs$"), "read"),
        ("GET", re.compile(r"^/docs/[^/]+$"), "read"),
        ("GET", re.compile(r"^/tickets$"), "read"),
        ("GET", re.compile(r"^/tickets/[^/]+$"), "read"),
        ("GET", re.compile(r"^/milestones$"), "read"),
        ("GET", re.compile(r"^/milestones/[^/]+/summary$"), "read"),
        ("GET", re.compile(r"^/crew$"), "read"),
        ("GET", re.compile(r"^/tickets/[^/]+/messages$"), "read"),
        ("GET", re.compile(r"^/assumptions$"), "read"),
        ("GET", re.compile(r"^/decisions$"), "read"),
        ("GET", re.compile(r"^/sessions$"), "read"),
        ("GET", re.compile(r"^/sessions/[^/]+$"), "read"),
        ("GET", re.compile(r"^/pickup$"), "read"),
    ]

    def _project_key_scope(method: str, path: str, project: str) -> str | None:
        """The scope a project key needs for this request, or None when a project key may not
        make it at all (any other route, or another project's)."""
        if (method, path) in _PK_ROUTES:
            return _PK_ROUTES[(method, path)]
        m = _PROJECT_PATH.match(path)
        if m and m.group(1) == project:
            rest = m.group(2) or ""
            for meth, pattern, scope in _PK_PROJECT_ROUTES:
                if meth == method and pattern.match(rest):
                    return scope
        return None

    def _project_key_denied(project: str) -> str:
        p = store.get_project(project)
        return f"this key only reads project {p['name'] if p else project}"

    # Auth off (no token) → no middleware, the instance is fully open (local default). Auth on →
    # every non-public route needs a credential carrying the required scope; ingest is gated exactly
    # like reads and management (admin implies all scopes).
    if deps.auth_token:
        @app.middleware("http")
        async def _guard(request, call_next):
            required = _required_scope(request.method, request.url.path)
            if required is not None:
                scopes, ident = deps.resolve_credential(request)
                if not scopes:
                    return JSONResponse({"detail": "authentication required"}, status_code=401)
                if ident.get("project"):
                    # a project key: only the allowlisted routes, only its own project
                    need = _project_key_scope(request.method, request.url.path, ident["project"])
                    if need is None or (need != "any" and need not in scopes):
                        return JSONResponse({"detail": _project_key_denied(ident["project"])},
                                            status_code=403)
                    request.state.project_key = ident["project"]
                elif required != "any" and required not in scopes and "admin" not in scopes:
                    return JSONResponse({"detail": f"this credential lacks the {required!r} scope"},
                                        status_code=403)
                request.state.credential = ident
                request.state.scopes = sorted(scopes)
            token = deps.caller_var.set(request)
            try:
                return await call_next(request)
            finally:
                deps.caller_var.reset(token)
