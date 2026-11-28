"""taresd: the always-on daemon. Owns the store; takes the session lines and GitHub App webhooks
in; serves the local HTTP API (the factory's routes and the management API) and the built-in
console UI. There is no source catalog (Tares 2.0, TR-1811).
"""
from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import json
import os
import time
import re
import secrets
import shutil
import traceback
import uuid
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               Response)
from pydantic import BaseModel

from . import loop as loop_mod
from .host import Host
from .envelope import now_utc
from .models import add_usage, empty_usage
from .providers import (DEFAULT_API_BASE, resolve_anthropic_headers, resolve_api_base,
                        resolve_provider)
from . import metrics as _metrics
from . import licence
from . import session_archive
from . import session_ingest
from . import session_digest, session_move
from . import team as team_mod
from . import docs as docs_mod
from . import factory as factory_mod
from . import memory_route
from .integrations.builtin import memory_tares
from . import answers as answers_mod, claude_usage, factory_api, guardrails_api, integrations_api, milestone_summary, project_secrets, morning_status as morning_mod, standup as standup_mod, waiting_slack as waiting_slack_mod
from . import edition as edition_mod
from . import factory_home, request_memo, screens
from . import report as report_mod
from . import linear as _linear
from .integrations import hooks as integrations_hooks, registry as integrations_registry, tickets as _tickets_integ
from . import github_tickets as _gh_tickets
from . import providers as providers_mod
from . import slack as slack_mod
from . import slack_verify
from . import timeline
from .slack import SETTING_KEY as SLACK_TOKEN_SETTING, resolve_token as resolve_slack_token
from .store import ReadExclude, Store, StoreUnavailable
from .factory_projects import Projects, ProjectError

SLACK_TEAM_SETTING = "slack_team"   # JSON {id, name}: the Slack team the bot token belongs to

DB_PATH = os.getenv("TARES_DB", "tares.duckdb")

# Auth mode. When set (via `tares up --auth`, which resolves + exports a root token), the whole
# API + console + ingest require a credential; when unset the instance is open (local default).
# The SPA shell and static assets stay public so the login screen can load. This is the ONE security
# switch — there is no separate ingest token and no read-only mode; producers get scoped API keys.
AUTH_TOKEN = os.getenv("TARES_AUTH_TOKEN", "").strip()
# Cloud login handoff (additive, env-gated). Set on a cloud-managed cell to the control plane's login
# URL, e.g. https://app.navflow.dev/login; unset for self-host (behaves exactly as before). Surfaced
# on the PUBLIC /health so a logged-out console knows where to send the browser to authenticate.
LOGIN_URL = os.getenv("TARES_LOGIN_URL", "").strip()
# The workspace this cell belongs to in the control plane (additive, env-gated; TR-142). Users,
# plan, storage and the Slack app install are managed there, not in the cell; the console links
# out to it so a user never has to know the control plane exists to find them. Unset for
# self-host: no link. Public, non-secret, surfaced on /health next to login_url.
WORKSPACE_URL = os.getenv("TARES_WORKSPACE_URL", "").strip()
# Cloud only: where "Connect GitHub" sends the browser (the control plane's install flow for the
# GlassFlow-owned App, TR-166). Set, Settings > GitHub offers it instead of "Create GitHub App".
GITHUB_CONNECT_URL = os.getenv("TARES_GITHUB_CONNECT_URL", "").strip()
# Cloud only: where "Connect Slack" sends the browser (the control plane's Slack install for this
# workspace, TR-364). Set, Settings > Slack offers Connect and Disconnect instead of the paste forms.
SLACK_CONNECT_URL = os.getenv("TARES_SLACK_CONNECT_URL", "").strip()
# Cloud only: where "Connect Linear" sends the browser (the control plane's sign-in with the
# GlassFlow Linear app, TR-537). Set, Settings > Linear offers Connect and Disconnect; the control
# plane hands the token to this cell and renews it.
LINEAR_CONNECT_URL = os.getenv("TARES_LINEAR_CONNECT_URL", "").strip()
# Cloud only: the control plane's list of the signed-in person's workspaces (TR-370). Set, the
# console's top left becomes a workspace switcher; the console fetches it with the person's
# control-plane session cookie, the cell never calls it.
WORKSPACES_URL = os.getenv("TARES_WORKSPACES_URL", "").strip()
# Cloud only: this workspace's own record in the control plane (TR-375), e.g.
# {cp}/api/workspaces/acme. Set, Settings gets a Workspace tab (team, plan, storage, credit,
# delete); the console calls it with the person's control-plane session cookie, the cell never does.
WORKSPACE_API_URL = os.getenv("TARES_WORKSPACE_API_URL", "").strip()
# Cloud only: the control plane's sign-out (TR-377). Set, Sign out forgets the workspace key and
# then goes there, so it ends the Tares Cloud session too. Unset: Sign out as before.
LOGOUT_URL = os.getenv("TARES_LOGOUT_URL", "").strip()
# The Anthropic key for the cell's summaries is resolved at request time via
# resolve_anthropic_headers(store): Resolve headers from the console-stored key,
# then ANTHROPIC_AUTH_TOKEN, then ANTHROPIC_API_KEY.
# Never returned by any API — capabilities exposes only a boolean.
# The Slack bot token behind the slack:// dispatch sink keeps the OPPOSITE order via
# resolve_slack_token(store): env TARES_SLACK_BOT_TOKEN wins over the console-stored value
# (infrastructure wiring, not a metered credential — see the note on resolve_token).



_STARTED_MONO = time.monotonic()


def _installed_version() -> str | None:
    from importlib.metadata import version as _v
    try:
        return _v("tares")
    except Exception:
        return None


def _is_ingest(path: str) -> bool:
    return path.startswith("/ingest/")


def _bearer(auth: str | None) -> str:
    return auth[7:].strip() if auth and auth.lower().startswith("bearer ") else ""


SLACK_EVENTS_PATH = "/api/slack/events"
SLACK_CHANNELS_TTL = 60.0         # seconds a good channel list is served from the cell
SLACK_CHANNELS_FAIL_TTL = 5.0     # ... and a failed one at least (longer on a 429's Retry-After)

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


_SIZE_UNITS = {"": 1, "k": 10**3, "m": 10**6, "g": 10**9, "t": 10**12,
               "ki": 1 << 10, "mi": 1 << 20, "gi": 1 << 30, "ti": 1 << 40}


def _parse_size(v: str | None) -> int | None:
    """TARES_MAX_DB_SIZE -> bytes. Accepts a plain integer or a Kubernetes-style quantity
    ('10Gi', '500Mi', '2G') so a Helm chart can pass the PVC size through verbatim. Unset,
    empty or unparseable -> None, i.e. no denominator and exactly today's behaviour."""
    if not v or not (v := v.strip()):
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGTkmgt]i?|)[Bb]?", v)
    if not m:
        return None
    n = int(float(m.group(1)) * _SIZE_UNITS[m.group(2).lower()])
    return n if n > 0 else None


# The operator's explicit limit; None means "the volume the data directory sits on", which is
# what a cell wants: a grown volume is reflected on the next mount, no chart value to refresh.
MAX_DB_SIZE = _parse_size(os.getenv("TARES_MAX_DB_SIZE"))
# Above this share of the limit ingest is refused (507), so the database never
# fills its disk and dies (glassflow-web, 2026-09-08). Reads, findings and the console keep working.
INGEST_PAUSE_PCT = float(os.getenv("TARES_INGEST_PAUSE_PCT", "95"))

from ._ui import NO_CONSOLE_HINT, console_built, ui_dist as _ui_dist  # noqa: E402  (kept under this name: tests and callers read it here)


UI_DIST = _ui_dist()

# What this cell can do, listed on /health so the crew CLI and the plugin can refuse clearly when the
# cell is too old for a command. Later tickets append their names (merge_queue, guardrail_rules,
# launch_spec, recall). A daemon that could not start lists none. Kept on one line: the crew parity
# scenarios read the list from this file.
CAPABILITIES = ["integrations", "merge_queue", "guardrail_rules", "hook_points", "guardrail_pairs", "recall", "launch_spec", "github_token"]

# /health reports `degraded` at or above this share of the storage limit. On the 0-100 scale of
# /api/usage's pct_used, so 90 means 90% — not 0.9. Unknown (no limit and no volume) is never degraded.
DEGRADED_PCT = float(os.getenv("TARES_DEGRADED_PCT", "90"))


def storage_limit(db_path: str) -> tuple[int | None, str]:
    """(max_bytes, where it comes from): TARES_MAX_DB_SIZE when set ("env"), else the total size
    of the volume the database sits on ("volume"). None only when neither is known."""
    if MAX_DB_SIZE:
        return MAX_DB_SIZE, "env"
    try:
        return shutil.disk_usage(os.path.dirname(os.path.abspath(db_path)) or ".").total, "volume"
    except OSError:
        return None, ""


def storage_state(store, db_path: str) -> dict:
    """How full this instance is against its limit, and whether ingest is paused because of it.
    stat() calls only, so every probe and every ingest can afford it."""
    limit, origin = storage_limit(db_path)
    if not limit:
        return {"max_bytes": None, "max_bytes_source": "", "pct_used": None, "paused": False, "detail": None}
    pct = round(100 * store.disk_bytes() / limit, 2)
    paused = pct >= INGEST_PAUSE_PCT
    detail = None
    if paused:
        detail = (f"ingest paused: storage {pct}% full of the {limit} byte "
                  f"{'limit' if origin == 'env' else 'volume'}; grow the volume or delete data")
    elif pct >= DEGRADED_PCT:
        detail = f"storage {pct}% full ({limit} byte {'limit' if origin == 'env' else 'volume'})"
    return {"max_bytes": limit, "max_bytes_source": origin, "pct_used": pct, "paused": paused,
            "detail": detail}


def _serve_ui(path: str):
    """The built console SPA: a real file if it exists, else index.html (client-side routing)."""
    if not console_built(UI_DIST):
        return JSONResponse({"detail": f"console not built: {NO_CONSOLE_HINT}"}, status_code=404)
    f = (UI_DIST / path).resolve()
    if path and f.is_file() and f.is_relative_to(UI_DIST.resolve()):
        return FileResponse(f)
    return FileResponse(UI_DIST / "index.html")


def _state_db_unreachable(e: BaseException) -> bool:
    """True when the store failed because the Postgres state database (TARES_STATE_DSN) could not
    be reached or refused the login: no DNS, a connect timeout, no connection slots left. Those
    pass on their own, unlike a locked or corrupt DuckDB file or a state database that does not
    hold this cell's state."""
    try:
        import psycopg
    except ImportError:
        return False
    x = e.__cause__ or e.__context__
    while x is not None:
        if isinstance(x, psycopg.OperationalError):
            return True
        x = x.__cause__ or x.__context__
    return False


def _degraded_app(reason: str, retry: bool = False) -> FastAPI:
    """The app we serve when the store could not be opened. It exists so the failure reaches a
    human instead of ERR_CONNECTION_REFUSED: the console still loads and every data route answers
    503 with the reason, so the UI can name the problem. No ingest: nothing that would need the
    store. Restart once the DB is reachable again.

    `retry`: the failure passes on its own (_state_db_unreachable), so /health answers 503 and the
    liveness probe restarts the daemon, which tries the database again. Left at 200, a cell that
    started during a DNS outage or a burst of restarts that filled the database's connection slots
    stayed degraded until someone restarted it by hand (2026-10-31: twelve cells)."""
    app = FastAPI(title="taresd (degraded)")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    @app.get("/health", include_in_schema=False)
    def health():
        # HTTP 200 with status "down": a probe that can read the body learns WHAT is wrong, and the
        # console (which only ever reaches this daemon) can render it. Keys AuthGate depends on stay.
        body = {"status": "down", "detail": reason, "auth_required": bool(AUTH_TOKEN),
                "sources": [], "pct_used": None, "capabilities": []}
        if LOGIN_URL:
            body["login_url"] = LOGIN_URL
        return JSONResponse(body, status_code=503) if retry else body

    async def unavailable():
        return JSONResponse({"detail": f"database unavailable; {reason}", "status": "down"},
                            status_code=503)

    # Everything that needs the store answers 503 (not a bare 500, and not the SPA's index.html).
    for _p in ("/api/{rest:path}", "/ingest/{rest:path}"):
        app.add_api_route(_p, unavailable, include_in_schema=False,
                          methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])

    @app.get("/{path:path}", include_in_schema=False)
    def ui(path: str):
        # An unmatched /api/* or /ingest/* path must never answer with the SPA's HTML and a 200 —
        # a client checking the status reads that as success and fails to parse (TR-226). The
        # console never requests these prefixes, so nothing legitimate is lost.
        if path == "api" or path.startswith(("api/", "ingest/")):
            raise HTTPException(status_code=404, detail=f"unknown API path /{path}")
        return _serve_ui(path)

    return app


class ProjectIn(BaseModel):
    # `template`, `params` and `objects` are accepted and ignored: a project has no template
    # since Tares 2.0 (TR-1812), and older clients still send them
    name: str = ""
    goal: str | None = None       # one line, at most 200 characters
    kind: str | None = None       # "software_factory" for a project a spec session makes
    icon: str | None = None       # one emoji shown beside the name; none = no icon


class ProjectUpdate(BaseModel):
    name: str = ""         # blank = unchanged
    # the project's goal; left out = unchanged, "" or null = cleared
    goal: str | None = None
    # the project's icon, one emoji; left out = unchanged, "" or null = removed
    icon: str | None = None


class GithubCredentialIn(BaseModel):
    name: str
    token: str = ""              # secret; blank-to-keep on update
    api_url: str | None = None   # GitHub Enterprise API base; empty = github.com; omitted on
                                 # update = keep
    # token (default) | app (an App entered by hand) | app_broker (Tares Cloud's control plane
    # holds the App's key and hands this cell tokens; see github_app.py)
    kind: str = "token"
    app_id: str = ""
    private_key: str = ""        # secret; blank-to-keep on update
    webhook_secret: str = ""     # secret; blank-to-keep on update
    installation_ids: list[int] = []
    token_url: str = ""          # app_broker: where the cell asks for a token
    broker_secret: str = ""      # app_broker: the cell's own bearer to the broker; blank-to-keep
    installation_id: int | None = None   # app_broker: the one installation
    account: str = ""            # app_broker: the installation's account login
    # app_broker: the repositories (owner/repo) this workspace follows in the installation
    # (TR-372); replaced wholesale when present, kept when omitted on update
    repositories: list[str] | None = None


class GithubAppCreateIn(BaseModel):
    name: str = "github-app"     # the credential's name in Tares
    org: str = ""                # GitHub organization to create the App in; empty = your account
    app_name: str = ""           # the App's name on GitHub (globally unique); empty = suggested
    public_url: str = ""         # this cell's address as GitHub reaches it; empty = this request's


class EditionIn(BaseModel):
    edition: str


class AnthropicKeyIn(BaseModel):
    key: str    # blank-to-keep is not offered here: the only edits are "set a new one" or DELETE


class GatewayIn(BaseModel):
    url: str
    token: str = ""    # blank keeps the stored token; the URL alone can change


class ProviderIn(BaseModel):
    kind: str                # anthropic | openai | openai_compatible
    name: str = ""           # display name; names an OpenAI-compatible entry (its id is the slug)
    key: str = ""            # blank keeps the stored credential
    base_url: str = ""       # optional for anthropic/openai, required for openai_compatible


class ProviderDefaultIn(BaseModel):
    id: str


class SlackTokenIn(BaseModel):
    token: str  # same contract as AnthropicKeyIn: set a new one, or DELETE to clear


class SlackSigningSecretIn(BaseModel):
    secret: str  # the signing secret behind POST /api/slack/events; same write-only contract


class SlackChannelsChangedIn(BaseModel):
    event: str = ""      # the Slack event behind it (member_joined_channel, channel_rename, ...)
    channel: str = ""    # the channel it concerns; informational, the whole list is re-read


class SlackTeamIn(BaseModel):
    team_id: str     # the Slack workspace the bot token belongs to, e.g. T0123ABC
    team_name: str = ""


def make_app() -> FastAPI:
    try:
        # TARES_STATE_DSN (a cloud cell): the state lives in that Postgres database, the events
        # stay in DuckDB. Unset, everything is in the DuckDB file as before.
        store = Store(DB_PATH, state_dsn=os.getenv("TARES_STATE_DSN") or None)
    except StoreUnavailable as e:
        # Losing the DB must not cost us the ability to SAY that we lost the DB. Log the full
        # traceback (degraded mode explains the failure to the user, it does not hide it from the
        # operator) and serve the console + 503s instead of exiting before uvicorn ever binds.
        traceback.print_exc()
        if _state_db_unreachable(e):
            print(f"taresd: DEGRADED; {e.reason}. Serving the console and 503s; /health answers "
                  f"503 too, so the liveness probe restarts this daemon to try again.", flush=True)
            return _degraded_app(e.reason, retry=True)
        print(f"taresd: DEGRADED; {e.reason}. Serving the console and 503s; fix the database "
              f"and restart.", flush=True)
        return _degraded_app(e.reason)

    # The edition is settled here, before the seed project below: a seeded empty cell would
    # otherwise look like a cell with data and be settled as platform.
    edition_mod.settle(store)

    # The sessions table catches up with the events in the background (TR-1471); the lines stored
    # from here on are counted as they arrive.
    store.start_session_reconcile()
    # the factory edition's recent sessions are rebuilt in the session store, once (TR-1609)
    session_move.start(store)
    # factory edition: the times of the stored lines are read into memory in the background, so the
    # first trigger and page do not wait for it (TR-1695)
    session_digest.start(store)

    projects = Projects(store)

    @asynccontextmanager
    async def lifespan(_app):
        # the daemon's own counters (/metrics, TR-308): storage gauges read on scrape, the event
        # loop watched by a one-second timer for the whole life of the process
        _metrics.set_storage_probe(lambda: {**store.storage_usage(), **{
            k: v for k, v in storage_state(store, DB_PATH).items()
            if k in ("max_bytes", "pct_used", "paused")}})
        try:
            from importlib.metadata import version as _pkg_version
            _metrics.set_info(_pkg_version("tares"))
        except Exception:
            _metrics.set_info(None)
        _metrics.prime(providers=[p["id"] for p in providers_mod.list_providers(store)["providers"]])
        _metrics.set_licence_probe(licence.peek)
        # providers the environment handed the cell list their models now, not on a click
        asyncio.create_task(providers_mod.discover_env_entries(store))
        loop_stop = asyncio.Event()
        loop_mod.attach(asyncio.get_running_loop())   # handlers in threads start work here
        loop_watch = asyncio.create_task(_metrics.watch_event_loop(loop_stop))
        # pull requests read again in the background, not only when a project is opened (TR-1010)
        pr_sync_task = asyncio.create_task(factory_api.sync_prs(store, loop_stop))
        # the morning status goes out from here, whoever's laptop is shut (TR-825)
        morning_task = asyncio.create_task(_morning.run(loop_stop))
        # the licence: checked again every minute, and the desk told as it nears its end (TR-912)
        licence_task = asyncio.create_task(licence.run(store, loop_stop))
        waiting_task = asyncio.create_task(_waiting_slack.run(loop_stop))
        # sessions idle for a week move from the session store to the archive, once an hour (TR-1506)
        archive_task = asyncio.create_task(session_archive.run(store, loop_stop))
        # the weekly figures the report reads, built once and then kept current (TR-1666)
        report_task = asyncio.create_task(report_mod.run(store, loop_stop))
        # Linear and GitHub Issues tickets kept in step by their integrations (TR-1806)
        from .integrations import sync as integration_sync
        sync_task = asyncio.create_task(integration_sync.run(store, loop_stop))
        print("taresd: console at / · management API at /api")
        yield
        loop_stop.set()
        loop_watch.cancel()
        pr_sync_task.cancel()
        morning_task.cancel()
        licence_task.cancel()
        waiting_task.cancel()
        archive_task.cancel()
        report_task.cancel()
        sync_task.cancel()

    # the interactive API reference lives under /api so /docs is the console's Docs and memory page
    app = FastAPI(title="taresd", lifespan=lifespan, docs_url="/api/reference", redoc_url=None)
    app.state.store = store   # for in-process tests; DuckDB is single-writer, so no second Store
    integrations_hooks.bind(store)   # the guardrail hooks resolve a project's guardrails from it
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    # One answer for an action that needs a licence the cell does not have (TR-912). The three
    # keys sit at the top of the body, not under `detail`, so a client can read `code` directly.
    @app.exception_handler(licence.LicenceRequired)
    async def _licence_required(_request, e: licence.LicenceRequired):
        if licence.hosted():
            # a Tares Cloud cell: plain words whatever the reason, and no page to read about it
            return JSONResponse({"code": "licence_required", "message": licence.CLOUD_WORDS,
                                 "url": ""}, status_code=402)
        return JSONResponse({"code": "licence_required", "message": e.message,
                             "url": licence.LICENCE_URL}, status_code=402)

    licence.current(store, force=True)

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

    def _pk(request: Request) -> str | None:
        """The project of the request's project key, or None for any other credential (and on an
        open instance, where no key is checked)."""
        return getattr(request.state, "project_key", None)

    # Licence-independent by design (TR-915): never consult licence or team here.
    def _resolve_credential(request) -> tuple[set, dict] | tuple[None, None]:
        """Token from the request -> (scopes, identity), or (None, None) if unknown/absent."""
        tok = _bearer(request.headers.get("authorization")) or request.headers.get("x-tares-token", "")
        if not tok:
            return None, None
        if AUTH_TOKEN and tok == AUTH_TOKEN:
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
                     "person": _key_person(key)}
            if key.get("project"):
                ident["project"] = key["project"]
            return set(key["scopes"]), ident
        return None, None

    def _person_ref(p: dict | None) -> dict | None:
        """The part of a person a credential carries."""
        return ({"id": p["id"], "name": p["name"], "email": p["email"],
                 "picture": p.get("picture")} if p else None)

    # Licence-independent by design (TR-915): never consult licence or team here.
    def _key_person(key: dict) -> dict | None:
        """Who a key acts as: the person it is bound to. A key nobody has bound acts as `local`
        while `local` is the only person on the cell (a single-user cell keeps working), and as
        nobody once a second person exists."""
        if key.get("person_id"):
            return _person_ref(store.get_person(key["person_id"]))
        if store.only_local_person():
            return _person_ref(store.get_person("local"))
        return None

    # Licence-independent by design (TR-915): never consult licence or team here.
    def _current_person(request) -> dict | None:
        """The person making this request ({id, name, email}), or None for the system credential
        and for a key with no person. Always read from the credential, never from a header or the
        body. With auth off it is the person `local`."""
        ident = getattr(request.state, "credential", None)
        if ident is not None:
            return ident.get("person")   # resolved once, by the guard
        # auth off: look `local` up once per request and keep it on the request
        if AUTH_TOKEN:
            return None
        if not hasattr(request.state, "person"):
            request.state.person = _person_ref(store.get_person("local"))
        return request.state.person

    def _is_admin(request) -> bool:
        """The credential may manage people and keys: admin scope (the system credential has it),
        or auth off, where everyone is the admin."""
        if not AUTH_TOKEN:
            return True
        return "admin" in (getattr(request.state, "scopes", None) or [])

    def _is_system(request) -> bool:
        """The system credential (the root TARES_AUTH_TOKEN, which is no person), or auth off,
        where the cell belongs to the one person using it."""
        if not AUTH_TOKEN:
            return True
        return (getattr(request.state, "credential", None) or {}).get("kind") == "system"

    # Who may see a private project (TR-625). The guard below keeps the request being served here,
    # so the project helpers that ~70 routes already call can check it without taking a request
    # argument. Auth off has no guard, so nothing is set and every project is visible.
    _CALLER: contextvars.ContextVar = contextvars.ContextVar("tares_caller", default=None)

    # Licence-independent by design (TR-915): never consult licence or team here.
    def _can_see(p: dict, request=None) -> bool:
        """May this caller see project `p`? A team project: anyone who gets this far. A private
        one: the system credential, a project key for it, and the people on it (its lead and its
        members). An admin who is none of those does not; they can only hand the lead over (see
        set_project_access). Rights come from the credential, never from people.is_admin (A9)."""
        request = request or _CALLER.get()
        if request is None or p.get("scope") != "private":
            return True
        # A caller that knows no person can call as the system credential with this header:
        # every private project is then hidden from it. Only the system
        # credential can narrow itself this way; anyone else's header is ignored.
        if _is_system(request) and request.headers.get("x-tares-viewer") != "public":
            return True
        if _pk(request) == p["id"]:
            return True
        person = _current_person(request)
        return bool(person and store.is_project_member(p["id"], person["id"]))

    # Licence-independent by design (TR-915): never consult licence or team here.
    def _hidden_projects() -> list[dict]:
        """The private projects this caller cannot see; usually none, and then one small query
        is all a cell-wide read adds (TR-629)."""
        return [p for p in store.private_projects() if not _can_see(p)]

    # Licence-independent by design (TR-915): never consult licence or team here.
    def _hidden_ids() -> set[str]:
        """The projects the caller cannot see: the private ones they are not on and, for every
        caller, the private ones that were deleted (TR-1697)."""
        private, deleted = store.private_and_deleted()
        return {p["id"] for p in private if not _can_see(p)} | deleted

    # Licence-independent by design (TR-915): never consult licence or team here.
    def _read_exclude() -> dict | None:
        """What event reads leave out: the rows of the projects the caller cannot see, the
        deleted private ones among them for every caller."""
        private, deleted, records = store.private_and_records()
        hp = [p for p in private if not _can_see(p)]
        if not hp and not deleted:
            return None
        return ReadExclude(
            {"ids": [p["id"] for p in hp] + sorted(deleted), "names": [p["name"] for p in hp],
             "shared": []}, records)

    # Licence-independent by design (TR-915): never consult licence or team here.
    def _owned_by_hidden(kind: str, name: str, hidden: set[str]) -> bool:
        """An object whose every project is hidden from the caller (one it shares with a project
        they can see stays visible)."""
        if not hidden:
            return False
        uids = store.projects_using(kind, name)
        return bool(uids) and set(uids) <= hidden

    # Auth off (no token) → no middleware, the instance is fully open (local default). Auth on →
    # every non-public route needs a credential carrying the required scope; ingest is gated exactly
    # like reads and management (admin implies all scopes).
    if AUTH_TOKEN:
        @app.middleware("http")
        async def _guard(request, call_next):
            required = _required_scope(request.method, request.url.path)
            if required is not None:
                scopes, ident = _resolve_credential(request)
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
            token = _CALLER.set(request)
            try:
                return await call_next(request)
            finally:
                _CALLER.reset(token)

    # per-route timing in /metrics (TR-652): added after every other middleware so it is the
    # outermost one and times auth rejections too
    app.add_middleware(_metrics.RequestTiming)

    def _err(e: Exception, code: int = 400):
        raise HTTPException(status_code=code, detail=str(e))

    # ── agent surface (unchanged contract; queries now logged) ───────────────
    # A scrape token for /metrics (TR-653), separate from the cell's own auth token on purpose: the
    # scraper gets only the counters. Read here, not at import, so each app built reads the env.
    metrics_token = os.getenv("TARES_METRICS_TOKEN", "").strip()

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint(request: Request):
        """The daemon's own counters in the Prometheus text format (TR-308): events per source,
        model calls and spend by provider, storage against its limit, event loop lag. Counts only. Public like /health
        unless TARES_METRICS_TOKEN is set; then it wants `Authorization: Bearer <that token>`
        (TR-653). The auth guard lets /metrics through either way, so this check decides."""
        if metrics_token and not hmac.compare_digest(
                request.headers.get("authorization", "").encode(),
                f"Bearer {metrics_token}".encode()):
            return JSONResponse({"detail": "this endpoint needs the metrics scrape token"},
                                status_code=401, headers={"WWW-Authenticate": "Bearer"})
        try:
            body, ctype = _metrics.render()
        except RuntimeError:
            _err(KeyError("metrics need the prometheus-client package"), 404)
        return Response(content=body, media_type=ctype)

    @app.get("/health")
    def health():
        """Liveness that actually touches the store, so a wedged DuckDB can't read as healthy to a
        k8s probe or the control plane's uptime check. `ok` / `degraded` (running, but nearly out of
        the configured storage) / `down` (the store stopped answering — restart needed). Always
        HTTP 200: the status and `detail` say what is wrong, which a bare 503 could not.
        `pct_used` is on /api/usage's 0-100 scale and is null when no limit is configured — unknown,
        never 0. The probe is a SELECT 1 plus a stat() of the db file; no table is scanned."""
        status, detail, pct = "ok", None, None
        try:
            store.ping()
        except Exception as e:
            status, detail = "down", f"database unavailable: {e}"
        if status == "ok":
            st = storage_state(store, DB_PATH)
            pct = st["pct_used"]
            if st["detail"]:
                status, detail = "degraded", st["detail"]
        body = {"status": status, "auth_required": bool(AUTH_TOKEN), "sources": [],
                "pct_used": pct, "version": _installed_version(),
                "capabilities": list(CAPABILITIES),
                "uptime_seconds": int(time.monotonic() - _STARTED_MONO)}
        if detail:
            body["detail"] = detail
        if LOGIN_URL:
            # public, non-secret: where the logged-out console sends the browser to authenticate.
            body["login_url"] = LOGIN_URL
        if WORKSPACE_URL:
            body["workspace_url"] = WORKSPACE_URL
        if GITHUB_CONNECT_URL:
            body["github_connect_url"] = GITHUB_CONNECT_URL
        if SLACK_CONNECT_URL:
            body["slack_connect_url"] = SLACK_CONNECT_URL
        if LINEAR_CONNECT_URL:
            body["linear_connect_url"] = LINEAR_CONNECT_URL
        if WORKSPACES_URL:
            body["workspaces_url"] = WORKSPACES_URL
        if WORKSPACE_API_URL:
            body["workspace_api_url"] = WORKSPACE_API_URL
        if LOGOUT_URL:
            body["logout_url"] = LOGOUT_URL
        return body

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

    # ── push ingestion: the runtimes' session lines and the GitHub App's webhook ──
    async def _parse_ingest_body(request: Request):
        """Accept a JSON object/array OR NDJSON (one JSON object per line). An empty body (a
        ping) parses to []."""
        raw = (await request.body()).decode("utf-8", "replace").strip()
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            try:
                return [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
            except json.JSONDecodeError:
                _err(ValueError("body must be JSON or NDJSON"))

    def _refuse_if_full() -> None:
        """507 Insufficient Storage when the store is at the pause mark: the producer gets a plain
        reason and can retry later; the database keeps working for everything else."""
        st = storage_state(store, DB_PATH)
        if st["paused"]:
            raise HTTPException(status_code=507, detail=st["detail"])

    async def _store(source: str, envelopes: list) -> int:
        """Store a batch of events off the event loop and count it on /metrics."""
        await asyncio.to_thread(store.append, envelopes)
        _metrics.events_ingested(source, len(envelopes))
        return len(envelopes)

    async def _ingest_runtime(name: str, request: Request):
        """A runtime's intake (TR-1804): the runtime slot's integration `name` makes session lines
        of the body, and they are stored under the session source."""
        from .integrations import registry as integrations_registry
        from .session_map import SOURCE as SESSION_SOURCE
        _refuse_if_full()
        body = await _parse_ingest_body(request)
        person = _current_person(request)   # from the credential, never from the body
        cls = integrations_registry.get("runtime", name)
        if cls is None:
            raise HTTPException(status_code=503, detail=f"the {name} runtime is not loaded on this "
                                                        "cell, so its lines cannot be stored now")
        try:
            envs = cls().push_lines(body, store=store, person_id=person["id"] if person else None)
            n = await _store(SESSION_SOURCE, envs)
        except ValueError as e:
            _err(e, 400)
        except session_ingest.LinesNotStored as e:
            _err(e, 503)         # not acknowledged: the sender keeps the lines and sends them again
        return JSONResponse({"ingested": n}, status_code=202)

    @app.post("/ingest/claude_code", status_code=202)
    async def ingest_claude_code(request: Request):
        """Transcript lines from the Claude Code plugin, as NDJSON, a JSON object or an array."""
        return await _ingest_runtime("claude_code", request)

    @app.post("/ingest/codex", status_code=202)
    async def ingest_codex(request: Request):
        """What a crew's recorder read of its Codex stations (TR-1595)."""
        return await _ingest_runtime("codex", request)

    async def _ingest_github(row: dict, request: Request):
        """A GitHub App webhook delivery on its key (TR-1807): the ci_github integration's. Checked
        with the App credential's webhook secret on the raw body, mapped, stored, its pull
        requests kept in pr_events and every event told to the GitHub subscribers (the brain
        sync)."""
        from . import github_webhook
        from .webhook_verify import verify
        source = row.get("source") or "github"
        cred = store.get_github_credential(row.get("credential") or "") or {}
        secret = str((cred.get("config") or {}).get("webhook_secret") or "")
        reason = verify("github", secret, await request.body(), dict(request.headers), None)
        if reason:
            github_webhook.count_rejected(source, reason)
            return JSONResponse({"detail": f"signature check failed (github): {reason}"},
                                status_code=401)
        body = await _parse_ingest_body(request)
        try:
            envs = github_webhook.GithubAppDeliveries(
                store, source, row.get("credential") or "").map_payload(
                body, headers=dict(request.headers))
        except ValueError as e:
            _err(e, 400)
        n = await _store(source, envs)
        if envs:
            github_webhook.after_store(store, source, envs)   # pr_events and the GitHub hub
        return JSONResponse({"ingested": n}, status_code=202)

    @app.post("/ingest/{token}", status_code=202)
    async def ingest(token: str, request: Request):
        """A GitHub App's webhook delivery, on the key its hook URL carries (integration_keys).
        Any other key answers 404."""
        _refuse_if_full()
        try:
            keyed = await asyncio.to_thread(store.integration_key, token)
        except Exception:   # noqa: BLE001  (a store that cannot answer: the delivery is retried)
            raise HTTPException(status_code=503, detail="the store could not look the key up now")
        if keyed is None or keyed.get("integration") != "ci_github":
            _err(KeyError(f"unknown ingest key {token!r}"), 404)
        return await _ingest_github(keyed, request)

    # (there is no /api/security — whether auth is on is a boolean on /health; producers authenticate
    #  with scoped ingest API keys, not a shared token.)

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

    @app.get("/api/keys")
    def list_keys():
        hidden = _hidden_ids()
        return {"keys": [_key_out(k) for k in store.list_api_keys()
                         if not _key_hidden(k, hidden)],
                "enforced": bool(AUTH_TOKEN),   # without a root auth token the instance is open
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
                            h.licence_required(why)
                return store.person_or_create(
                    spec["email"], spec.get("name"), via="cloud" if system else "key",
                    owner=owner)["id"]
        except ValueError as e:
            _err(e, 409 if "already belongs" in str(e) else 400)

    @app.post("/api/keys", status_code=201)
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

    @app.put("/api/keys/{kid}/person")
    def set_key_person(kid: str, request: Request, body: dict = Body(...)):
        """Bind a key to a person: {person_id} or {person: {email, name}}. Admin."""
        if not body.get("person_id") and not body.get("person"):
            _err(ValueError("say which person: {person_id} or {person: {email, name}}"))
        key = store.get_api_key(kid)
        if not key or key["revoked_at"] or _key_hidden(key, _hidden_ids()):
            _err(KeyError(f"no active key {kid!r}"), 404)
        store.set_key_person(kid, _body_person(request, body))
        return _key_out(store.get_api_key(kid))

    @app.delete("/api/keys/{kid}")
    def revoke_key(kid: str):
        if _key_hidden(store.get_api_key(kid), _hidden_ids()) or not store.revoke_api_key(kid):
            _err(KeyError(f"no active key {kid!r}"), 404)
        return {"ok": True, "note": "key revoked"}

    def _edition_out() -> dict:
        value, needed = edition_mod.edition(store)
        return {"edition": value, "edition_name": edition_mod.NAMES[value], "edition_needed": needed}

    def _whoami_edition() -> dict:
        out = _edition_out()
        return out

    # ── People: who is on the cell. Keys belong to them; the credential names who is calling ──
    def _person_out(p: dict) -> dict:
        return {k: p.get(k) for k in ("id", "name", "email", "emails", "github", "is_admin",
                                      "created_via", "picture")}

    @app.get("/api/people")
    def list_people():
        return {"people": [_person_out(p) for p in store.list_people()]}

    @app.post("/api/people")
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
                        h.licence_required(why)
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

    @app.delete("/api/people/{pid}")
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

    @app.patch("/api/people/{pid}")
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
                h.licence_required("Changing other people needs an enterprise licence.")
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

    @app.get("/api/whoami")
    def whoami(request: Request):
        ident = getattr(request.state, "credential", None)
        scopes = getattr(request.state, "scopes", None)
        if ident is None:   # guard not active (open instance) or ingest-path credential
            return {"id": "open", "name": "no auth configured", "kind": "open",
                    "person": _current_person(request), "scopes": sorted(_SCOPES), **_whoami_edition()}
        out = {**ident, "scopes": scopes or [], **_whoami_edition()}
        if ident.get("project"):   # a project key: the project it reads, by id and name
            out["project_name"] = (store.get_project(ident["project"]) or {}).get("name")
        return out

    @app.get("/api/capabilities")
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

    # ── GitHub credentials: a token or an App stored once, used by the crew and the integrations ──
    # Same write-only contract as the other credentials: the token is never returned, blank-to-keep
    # on update, and the console can list, test and delete.
    from .github_credentials import (APP_KINDS, forget_repos, get_token, list_repos_for, list_tree,
                                     redact as _gh_redact, test_credential)
    from . import github_app as _gh_app
    from . import github_webhook as _gh_webhook

    def _gh_users(name: str) -> dict:
        """Where a credential is used, so deleting one is an informed act. Nothing in the cell
        names a credential since the source catalog went (TR-1811): both lists are empty."""
        return {"sources": [], "mcp_servers": []}

    def _gh_deliveries(cred: dict) -> dict:
        """What the settings row shows about an App's webhook: its key, the name its events go
        under, last delivery, refusals, drops."""
        row = store.integration_key_for("ci_github", cred["name"])
        if not row:
            return {}
        src = row.get("source") or "github"
        st = _gh_webhook.STATS.get(src) or {}
        return {"source": src, "ingest_key": row["key"],
                "deliveries": {"received": st.get("deliveries", 0), "stored": st.get("stored", 0),
                               "last_at": st.get("last_at"), "last_event": st.get("last_event"),
                               "unknown_installation": st.get("unknown_installation", 0),
                               "rejected_signature": _gh_webhook.REJECTED.get(src, 0)}}

    def _gh_row(c: dict) -> dict:
        return {**_gh_redact(c), **_gh_users(c["name"]),
                **(_gh_deliveries(c) if c.get("kind") in APP_KINDS else {})}

    def _ensure_gh_app_key(name: str, ingest_key: str | None = None) -> dict:
        """The webhook key of an App credential (integration_keys, TR-1805), made when missing:
        {key, source}. Its events go under `github` when no other credential's key uses that name,
        else `github_<credential>`. A key given (an App recreated under the same name, whose hook
        URL carries the new key) replaces the old one."""
        row = store.integration_key_for("ci_github", name)
        if row and (not ingest_key or row["key"] == ingest_key):
            return row
        if row:
            src = row.get("source") or "github"
        else:
            taken = store.integration_key_sources("ci_github")
            src = "github" if "github" not in taken else \
                "github_" + re.sub(r"[^A-Za-z0-9_]", "_", name)
        key = ingest_key or f"github_app-{secrets.token_hex(8)}"
        store.put_integration_key(key, "ci_github", name, src)
        return {"key": key, "source": src}

    def _check_gh_name(name: str) -> str:
        name = (name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            _err(ValueError("name must be alphanumeric/_/-"))
        if store.get_github_credential(name) is not None:
            _err(ValueError(f"GitHub credential {name!r} already exists"), 409)
        return name

    def _gh_repositories(names: list[str]) -> list[str]:
        """An app_broker's picked repositories: trimmed, each owner/repo, in order, no repeats. At
        most 500, GitHub's limit for one token."""
        if len(names) > 500:
            _err(ValueError("repositories: at most 500"))
        out: list[str] = []
        seen: set[str] = set()
        for n in names:
            n = (n or "").strip()
            if len(n) > 200 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+", n):
                _err(ValueError(f"repositories: {n[:200]!r} is not owner/repo"))
            if n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
        return out

    @app.get("/api/integrations/github")
    def list_github_credentials():
        return {"credentials": [_gh_row(c) for c in store.list_github_credentials()]}

    @app.post("/api/integrations/github", status_code=201)
    async def create_github_credential(body: GithubCredentialIn):
        name = _check_gh_name(body.name)
        api_url = (body.api_url or "").strip()
        if body.kind == "app":
            # an App the person created by hand: the same credential the manifest flow stores
            if not (body.app_id.strip() and body.private_key.strip() and body.webhook_secret.strip()):
                _err(ValueError("app id, private key and webhook secret are required"))
            try:
                _gh_app.app_jwt(body.app_id.strip(), body.private_key.strip())
            except ValueError as e:
                _err(e)
            cfg = {"app_id": body.app_id.strip(), "private_key": body.private_key.strip(),
                   "webhook_secret": body.webhook_secret.strip(), "installations": []}
            cred = {"name": name, "kind": "app", "api_url": api_url, "config": cfg}
            for iid in body.installation_ids:
                try:
                    cfg = _gh_app.with_installation({**cred, "config": cfg},
                                                    await _gh_app.get_installation(cred, iid))
                except ValueError as e:
                    _err(e)
            store.upsert_github_credential(name, "", "app", api_url, "", config=cfg)
            hook = _ensure_gh_app_key(name)
            return {"ok": True, "account": "", "source": hook["source"], "ingest_key": hook["key"]}
        if body.kind == "app_broker":
            # Tares Cloud: the control plane keeps the App's key and brokers tokens (TR-166)
            if not (body.token_url.strip() and body.broker_secret.strip()
                    and body.installation_id and body.webhook_secret.strip()):
                _err(ValueError("token_url, broker_secret, installation_id and webhook_secret "
                                "are required"))
            cfg = {"token_url": body.token_url.strip(), "broker_secret": body.broker_secret.strip(),
                   "installation_id": int(body.installation_id),
                   "webhook_secret": body.webhook_secret.strip(),
                   "repositories": _gh_repositories(body.repositories or [])}
            store.upsert_github_credential(name, "", "app_broker", api_url, body.account.strip(),
                                           config=cfg)
            hook = _ensure_gh_app_key(name)
            return {"ok": True, "account": body.account.strip(), "source": hook["source"],
                    "ingest_key": hook["key"]}
        if body.kind != "token":
            _err(ValueError("kind must be token, app or app_broker"))
        if not body.token.strip():
            _err(ValueError("token is required"))
        account = ""
        try:   # best effort: a bad token is still stored, the Test button explains it
            account = (await test_credential(body.token.strip(), api_url or None))["login"]
        except ValueError:
            pass
        store.upsert_github_credential(name, body.token.strip(), "token", api_url, account)
        return {"ok": True, "account": account}

    @app.put("/api/integrations/github/{name}")
    async def update_github_credential(name: str, body: GithubCredentialIn):
        existing = store.get_github_credential(name)
        if existing is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        api_url = (existing.get("api_url") or "") if body.api_url is None else body.api_url.strip()
        account = existing.get("account") or ""
        kind = existing.get("kind") or "token"
        if kind in APP_KINDS:
            # rotate a secret (blank-to-keep); installations follow GitHub, never this form
            cfg = dict(existing.get("config") or {})
            for field in ("private_key", "webhook_secret", "broker_secret", "token_url"):
                val = getattr(body, field, "").strip()
                if val:
                    cfg[field] = val
            if body.app_id.strip():
                cfg["app_id"] = body.app_id.strip()
            if kind == "app_broker" and body.repositories is not None:
                cfg["repositories"] = _gh_repositories(body.repositories)
            if kind == "app" and body.private_key.strip():
                try:
                    _gh_app.app_jwt(cfg.get("app_id"), cfg["private_key"])
                except ValueError as e:
                    _err(e)
            store.upsert_github_credential(name, "", kind, api_url,
                                           body.account.strip() or account, config=cfg)
            forget_repos(name)
            return {"ok": True, "account": body.account.strip() or account}
        token = body.token.strip() or existing["token"]     # blank-to-keep
        if body.token.strip():
            try:
                account = (await test_credential(token, api_url or None))["login"]
            except ValueError:
                pass
        store.upsert_github_credential(name, token, kind, api_url, account)
        forget_repos(name)
        return {"ok": True, "account": account}

    @app.delete("/api/integrations/github/{name}")
    def delete_github_credential(name: str):
        if store.get_github_credential(name) is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        store.delete_github_credential(name)
        store.delete_integration_keys("ci_github", name)   # its webhook key stops working (TR-1807)
        forget_repos(name)
        return {"ok": True, **_gh_users(name)}

    @app.post("/api/integrations/github/{name}/test")
    async def test_github_credential(name: str):
        cred = store.get_github_credential(name)
        if cred is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        if cred.get("kind") in APP_KINDS:
            return await _test_gh_app(cred)
        try:
            info = await test_credential(cred["token"], cred.get("api_url") or None)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if info["login"] and info["login"] != cred.get("account"):
            store.upsert_github_credential(name, cred["token"], cred.get("kind") or "token",
                                           cred.get("api_url") or "", info["login"])
        return {"ok": True, **info}

    async def _test_gh_app(cred: dict) -> dict:
        """An App: resync its installations from GitHub (a self-held App can list them), then mint
        a token per installation and count its repos. The proof the whole chain works."""
        try:
            if cred["kind"] == "app":
                # keep what is known or on the owner's account; a stranger's install of a public
                # App is not ours (see github_app_installed)
                insts = [i for i in await _gh_app.list_installations(cred)
                         if _gh_app.may_add_installation(cred, i["id"], i["account"])]
                cfg = {**(cred.get("config") or {}), "installations": insts}
                store.update_github_credential_config(cred["name"], cfg)
                cred = {**cred, "config": cfg}
            rows = []
            for inst in _gh_app.installations(cred):
                repos = await _gh_app.installation_repos(cred, inst["id"], use_cache=False)
                rows.append({**inst, "repos": len(repos)})
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not rows:
            return {"ok": False, "error": "the App is not installed anywhere yet; install it on "
                                          "GitHub", "installations": []}
        return {"ok": True, "login": rows[0]["account"], "installations": rows}

    @app.get("/api/integrations/github/{name}/repos")
    async def github_credential_repos(name: str, query: str = ""):
        cred = store.get_github_credential(name)
        if cred is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        try:
            repos = await asyncio.wait_for(list_repos_for(cred, query), timeout=60)
        except ValueError as e:
            _err(e, 502)
        except (TimeoutError, asyncio.TimeoutError):
            _err(ValueError("GitHub took too long to list repositories"), 504)
        return {"repos": repos}

    @app.get("/api/integrations/github/{name}/tree")
    async def github_credential_tree(name: str, repo: str, ref: str = "", path: str = ""):
        cred = store.get_github_credential(name)
        if cred is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        if not repo or "/" not in repo:
            _err(ValueError("repo must be owner/name"))
        try:
            token = await get_token(store, name, repo)
            return await asyncio.wait_for(
                list_tree(token, repo, ref, path, cred.get("api_url") or None), timeout=30)
        except ValueError as e:
            _err(e, 502)
        except (TimeoutError, asyncio.TimeoutError):
            _err(ValueError("GitHub took too long to list the repository"), 504)

    # ── Create GitHub App: GitHub's manifest flow, then the install ──
    # 1. POST .../apps (admin) returns the manifest and where to POST it; the console submits it as
    #    a form, so the person lands on GitHub's "create App" page with everything filled in.
    # 2. GitHub sends the browser to .../apps/callback?code=...&state=...; the code is exchanged for
    #    the App's id, key and webhook secret, the credential and its webhook key are stored.
    # 3. The person installs it; GitHub sends the browser to .../apps/installed?installation_id=...
    # The two callbacks are public to the auth middleware (a redirect has no Tares key) and gated
    # by the signed state, or for the install by proving the installation belongs to our App.

    def _gh_settings(params: dict) -> RedirectResponse:
        from urllib.parse import urlencode
        return RedirectResponse(f"/settings?{urlencode({'tab': 'github', **params})}",
                                status_code=303)

    @app.post("/api/integrations/github/apps")
    def create_github_app(body: GithubAppCreateIn, request: Request):
        name = _check_gh_name(body.name)
        base = (body.public_url.strip() or os.getenv("TARES_PUBLIC_URL", "").strip()
                or str(request.base_url)).rstrip("/")
        ingest_key = f"github_app-{secrets.token_hex(8)}"
        hook_url = f"{base}/ingest/{ingest_key}"
        host = urlsplit(base).hostname or ""
        app_name = body.app_name.strip() or f"Tares {host.split('.')[0] or name}"
        state = _gh_app.sign_state(store, {"op": "create", "cred": name, "key": ingest_key})
        warning = None
        if host in ("localhost", "127.0.0.1", "0.0.0.0") or host.endswith(".local"):
            warning = (f"GitHub cannot reach {base}. The App can still be created, but its "
                       "webhooks will fail until Tares is reachable (a public address or a tunnel); "
                       "enter that address as the public URL.")
        return {"action": f"{_gh_app.manifest_action(body.org)}?state={state}",
                "manifest": json.dumps(_gh_app.manifest(app_name, base, hook_url)),
                "hook_url": hook_url, "warning": warning}

    @app.get("/api/integrations/github/apps/callback")
    async def github_app_created(code: str = "", state: str = ""):
        try:
            data = _gh_app.verify_state(store, state)
            if data.get("op") != "create" or not code:
                raise ValueError("this link is not a GitHub App creation callback")
            name = data["cred"]
            if store.get_github_credential(name) is not None:
                raise ValueError(f"GitHub credential {name!r} already exists")
            conv = await _gh_app.convert_manifest(code)
        except ValueError as e:
            return _gh_settings({"error": str(e)})
        cfg = {k: conv[k] for k in ("app_id", "slug", "client_id", "client_secret", "private_key",
                                    "webhook_secret", "html_url", "owner", "app_name")}
        cfg["installations"] = []
        store.upsert_github_credential(name, "", "app", "", conv["owner"], config=cfg)
        _ensure_gh_app_key(name, data["key"])
        return _gh_settings({"github": name, "event": "created"})

    @app.get("/api/integrations/github/{name}/install")
    def github_app_install_link(name: str):
        """The GitHub page that installs this App (with a signed state naming the credential)."""
        cred = store.get_github_credential(name)
        if cred is None or cred.get("kind") != "app" or not (cred.get("config") or {}).get("slug"):
            _err(KeyError(f"{name!r} is not a GitHub App created here"), 404)
        state = _gh_app.sign_state(store, {"op": "install", "cred": name})
        return {"url": _gh_app.install_url(cred["config"]["slug"], state)}

    @app.get("/api/integrations/github/apps/installed")
    async def github_app_installed(installation_id: int = 0, setup_action: str = "",
                                   state: str = ""):
        if setup_action == "request" or not installation_id:
            return _gh_settings({"event": "requested", "detail": "an organization owner has to "
                                 "approve the install on GitHub"})
        named = None
        if state:
            try:
                data = _gh_app.verify_state(store, state)
                named = data.get("cred") if data.get("op") == "install" else None
            except ValueError:
                named = None
        # Two gates. The installation must belong to one of our Apps (GET /app/installations/{id}
        # signed as each App succeeds only for its own). And someone allowed here must have asked
        # for it: our signed state (the install link is admin-only), or, for an install started on
        # GitHub's own page, the App owner's account or one already known. Without the second, a
        # stranger installing a public App would join this cell.
        apps = [c for c in store.list_github_credentials() if c.get("kind") == "app"
                and (named is None or c["name"] == named)]
        for cred in apps:
            try:
                inst = await _gh_app.get_installation(cred, installation_id)
            except ValueError:
                continue
            if named is None and not _gh_app.may_add_installation(cred, installation_id,
                                                                  inst.get("account", "")):
                return _gh_settings({"error": f"installation on {inst.get('account')!r} was not "
                                              f"added: start it from Settings > GitHub "
                                              f"(Install / Add an organization)"})
            store.update_github_credential_config(cred["name"],
                                                  _gh_app.with_installation(cred, inst))
            if inst.get("account") and not cred.get("account"):
                store.upsert_github_credential(cred["name"], "", "app", cred.get("api_url") or "",
                                               inst["account"])
            _gh_app.forget_repos(cred["name"], installation_id)
            return _gh_settings({"github": cred["name"], "event": "installed",
                                 "account": inst.get("account", "")})
        return _gh_settings({"error": f"installation {installation_id} does not belong to a "
                                      "GitHub App created on this Tares"})

    # ── the Anthropic key: a stored key wins, env is the fallback ────────────
    @app.get("/api/settings/anthropic-key")
    def get_anthropic_key():
        """Never returns the key — only whether one is resolvable and where it came from. A key
        saved here takes over from the deployment's env key; `env_overrides` is kept for API
        compatibility and can now only be true when no key is stored."""
        headers, origin = resolve_anthropic_headers(store)
        return {"configured": bool(headers), "source": origin,
                "stored": bool(store.get_setting("anthropic_key")),
                "env_overrides": origin.startswith("env:")}

    @app.put("/api/settings/anthropic-key")
    def set_anthropic_key(body: AnthropicKeyIn):
        key = body.key.strip()
        if not key:
            _err(ValueError("key is required (use DELETE to remove the stored key)"))
        store.set_setting("anthropic_key", key)
        _, origin = resolve_anthropic_headers(store)
        return {"ok": True, "source": origin}

    @app.delete("/api/settings/anthropic-key")
    def clear_anthropic_key():
        """Removing the stored key falls back to the deployment's env key when one exists — on a
        hosted trial cell that is the trial key, until its operator removes it too."""
        store.set_setting("anthropic_key", None)
        key, origin = resolve_anthropic_headers(store)
        return {"ok": True, "configured": bool(key), "source": origin}

    # ── the enterprise licence (TR-912) ──
    # Not under /api/settings: that prefix is admin even to read, and any signed-in person may see
    # whether the cell is licensed. A project key is refused by the allowlist. Writing needs admin.
    # The key text is never returned and never logged.
    def _licence_body(force: bool = False) -> dict:
        return licence.current(store, force=force)

    @app.get("/api/licence")
    def get_licence():
        return _licence_body()

    @app.put("/api/licence")
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

    @app.delete("/api/licence")
    def delete_licence():
        store.set_setting(licence.LICENCE_SETTING, None)
        print("taresd: licence key removed", flush=True)
        return _licence_body(force=True)

    # ── model access through a gateway (TR-281): stored on the cell like the key ──────────
    # Where model calls go, and the credential for it. Saved here they win over the environment,
    # so a cloud customer with a mandated proxy configures it on their own cell and the platform
    # key stops being used, the same as when they store their own key. The token is write-only.
    def _gateway_status() -> dict:
        url, origin = resolve_api_base(store)
        return {"configured": origin != "", "url": url if origin else "", "source": origin,
                "stored": bool(store.get_setting("gateway_url")),
                "token_stored": bool(store.get_setting("gateway_token")),
                "default_url": DEFAULT_API_BASE}

    @app.get("/api/settings/gateway")
    def get_gateway():
        return _gateway_status()

    @app.put("/api/settings/gateway")
    def set_gateway(body: GatewayIn):
        url = body.url.strip().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            _err(ValueError("gateway url must be an http(s) URL with a host, e.g. "
                            "https://llm-gateway.internal"))
        store.set_setting("gateway_url", url)
        if body.token.strip():
            store.set_setting("gateway_token", body.token.strip())
        return {"ok": True, **_gateway_status()}

    @app.delete("/api/settings/gateway")
    def clear_gateway():
        """Back to the environment's gateway if the deployment set one, else Anthropic."""
        store.set_setting("gateway_url", None)
        store.set_setting("gateway_token", None)
        return {"ok": True, **_gateway_status()}

    # ── model providers (TR-301): the list a cell holds, one of them the default ──────────
    # The Anthropic entry is the key + gateway settings above under another door; OpenAI and
    # OpenAI-compatible entries (LiteLLM, OpenRouter, Ollama, vLLM) live in one JSON setting.
    # Credentials are write-only: the listing says configured-or-not and where from.
    @app.get("/api/settings/providers")
    def get_providers():
        return providers_mod.list_providers(store)

    @app.put("/api/settings/providers/default")
    def set_default_provider(body: ProviderDefaultIn):
        try:
            providers_mod.set_default(store, body.id.strip())
        except KeyError as e:
            _err(e, 404)
        except ValueError as e:
            _err(e)
        return {"ok": True, **providers_mod.list_providers(store)}

    @app.put("/api/settings/providers/{provider_id}")
    async def save_provider(provider_id: str, body: ProviderIn):
        """`new` as the id creates an entry (its id is the kind, or the slug of the name for an
        OpenAI-compatible endpoint)."""
        try:
            pid = providers_mod.save_provider(store, "" if provider_id == "new" else provider_id,
                                             body.kind.strip(), body.name, body.key, body.base_url)
        except ValueError as e:
            _err(e)
        # The endpoint lists its own models: ask it now, so the picker is filled the moment the
        # entry is saved. A failure is recorded on the entry, not raised: the entry is saved
        # either way, the built-in list stands in, and the console shows why.
        try:
            discovery = await providers_mod.refresh_models(store, pid)
        except ValueError as e:   # no credential to ask with yet
            discovery = {"models": [], "error": str(e)}
        return {"ok": True, "id": pid, "discovery": discovery, **providers_mod.list_providers(store)}

    @app.post("/api/settings/providers/{provider_id}/models")
    async def refresh_provider_models(provider_id: str):
        """Re-read what the endpoint serves (the admin added a model, the key's allowance
        changed, a newer Claude model). The built-in list stands in while it cannot be read."""
        try:
            result = await providers_mod.refresh_models(store, provider_id)
        except KeyError as e:
            _err(e, 404)
        except ValueError as e:
            _err(e)
        return {"ok": True, **result, **providers_mod.list_providers(store)}

    @app.delete("/api/settings/providers/{provider_id}")
    def delete_provider(provider_id: str):
        known = {p["id"] for p in providers_mod.list_providers(store)["providers"]}
        if provider_id not in known:
            _err(KeyError(f"unknown provider {provider_id!r}"), 404)
        try:
            providers_mod.delete_provider(store, provider_id)
        except ValueError as e:
            _err(e)
        return {"ok": True, **providers_mod.list_providers(store)}

    # ── the cell's edition (TR-724): admin through the /api/settings prefix rule ──
    def _edition_settings() -> dict:
        return {**_edition_out(), "env_value": os.getenv(edition_mod.EDITION_ENV) or None,
                "choices": [{"id": e, "name": edition_mod.NAMES[e]} for e in edition_mod.EDITIONS]}

    @app.get("/api/settings/edition")
    def get_edition():
        return _edition_settings()

    @app.put("/api/settings/edition")
    def set_edition(body: EditionIn):
        value = edition_mod._valid(body.edition)
        if value is None:
            _err(ValueError("edition must be one of " + ", ".join(edition_mod.EDITIONS)))
        store.set_setting(edition_mod.EDITION_SETTING, value)
        return {"ok": True, **_edition_settings()}

    # ── the Slack bot token: same contract as the Anthropic key ──────────────
    # One token per instance, behind the cell's Slack posts. It is a credential, so it is
    # write-only over the API exactly like the model key: the console can learn THAT one resolves
    # and where from, never what it is.
    @app.get("/api/settings/slack-bot-token")
    def get_slack_token():
        token, origin = resolve_slack_token(store)
        return {"configured": bool(token), "source": origin,
                "stored": bool(store.get_setting(SLACK_TOKEN_SETTING)),
                "env_overrides": origin.startswith("env:"), "team": _slack_team()}

    # ── the Slack team the bot token belongs to (TR-365) ─────────────────────
    # Not a secret: the control plane stores it next to the token it pushes, so Settings can say
    # which Slack workspace this one posts to. DELETE clears it, on disconnect.
    def _slack_team() -> dict | None:
        raw = store.get_setting(SLACK_TEAM_SETTING)
        if not raw:
            return None
        try:
            t = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(t, dict):
            return None
        return {"id": t.get("id") or "", "name": t.get("name") or ""}

    @app.put("/api/settings/slack-team")
    def set_slack_team(body: SlackTeamIn):
        team_id = body.team_id.strip()
        if not team_id:
            _err(ValueError("team_id is required (use DELETE to clear the team)"))
        store.set_setting(SLACK_TEAM_SETTING,
                          json.dumps({"id": team_id, "name": body.team_name.strip()}))
        return {"ok": True, "team": _slack_team()}

    @app.delete("/api/settings/slack-team")
    def clear_slack_team():
        store.set_setting(SLACK_TEAM_SETTING, None)
        return {"ok": True, "team": None}

    @app.put("/api/settings/slack-bot-token")
    def set_slack_token(body: SlackTokenIn):
        token = body.token.strip()
        if not token:
            _err(ValueError("token is required (use DELETE to remove the stored token)"))
        if not token.startswith("xox"):
            # Caught here rather than on the first failed delivery: a pasted webhook URL or signing
            # secret would otherwise sit in the settings table looking configured.
            _err(ValueError("that does not look like a Slack bot token; it should start with "
                            "'xoxb-' (OAuth & Permissions → Bot User OAuth Token)"))
        store.set_setting(SLACK_TOKEN_SETTING, token)
        _drop_channels_cache(forget=True)
        _, origin = resolve_slack_token(store)
        return {"ok": True, "source": origin,
                **({"note": "an environment token takes precedence and is still in use"}
                   if origin.startswith("env:") else {})}

    @app.delete("/api/settings/slack-bot-token")
    def clear_slack_token():
        store.set_setting(SLACK_TOKEN_SETTING, None)
        _drop_channels_cache(forget=True)
        token, origin = resolve_slack_token(store)
        return {"ok": True, "configured": bool(token), "source": origin}

    # ── the channels the console's picker offers ─────────────────────────────
    # Every public channel plus the private ones the bot is in, so that picking where something
    # goes never means digging a channel ID out of Slack's own UI.
    #
    # ALWAYS 200, never an exception: the console branches on `reason` and says why there is
    # nothing to pick (not connected, reconnect for the scope, Slack did not answer). A Slack
    # outage, or the very common token issued before `channels:read`/`groups:read` were
    # requested, must not blank the page.
    #
    # Cached in the cell: an open picker re-reads every few seconds, and conversations.list is
    # rate limited per workspace. A good list is kept a minute, a failure a few seconds (so Try
    # again soon asks Slack again), or as long as Slack's Retry-After says on a 429. When a re-read
    # fails while a good list is known, that list is served with `stale: true` rather than an
    # error: a picker that worked a minute ago must not turn into "Slack did not answer".
    # POST /api/slack/channels/changed drops it when Slack says a channel changed, so a channel
    # Tares was just added to shows up within one poll.
    #
    # `gen` counts the drops. A read captures it before asking Slack and stores its answer only if
    # no drop came in meanwhile; otherwise that answer predates the change and would hide it for
    # a minute. It still goes back to the caller that asked, just not into the cache.
    _channels = {"token": "", "expires": 0.0, "resp": None, "good": None, "gen": 0}
    _channels_lock = asyncio.Lock()       # one Slack read at a time; pickers poll together

    def _drop_channels_cache(forget: bool = False) -> None:
        """Make the next read ask Slack. `forget` also drops the last good list (the token
        changed, so that list may belong to another Slack)."""
        _channels["gen"] += 1
        _channels["expires"] = 0.0
        if forget:
            _channels.update(token="", resp=None, good=None)

    @app.get("/api/slack/channels")
    async def list_slack_channels():
        token, _origin = resolve_slack_token(store)
        if not token:
            return {"channels": [], "reason": "no_token"}
        async with _channels_lock:
            if _channels["token"] != token:      # not the token the kept list was read with
                _channels.update(token=token, expires=0.0, resp=None, good=None)
            if _channels["resp"] is not None and _channels["expires"] > time.monotonic():
                return _channels["resp"]
            gen = _channels["gen"]
            try:
                channels, reason, detail, retry = await slack_mod.list_channels(token)
            except Exception as e:                  # belt and braces: list_channels swallows too
                channels, reason, detail, retry = [], "error", f"{type(e).__name__}: {e}"[:200], None
            if reason is None:
                out = {"channels": channels, "reason": None}
                ttl = SLACK_CHANNELS_TTL
            else:
                good = _channels["good"]
                out = ({**good, "stale": True, **({"detail": detail} if detail else {})} if good
                       else {"channels": [], "reason": reason, **({"detail": detail} if detail else {})})
                ttl = max(SLACK_CHANNELS_FAIL_TTL, retry or 0.0)
            if _channels["gen"] == gen and _channels["token"] == token:
                _channels.update(expires=time.monotonic() + ttl, resp=out)
                if reason is None:
                    _channels["good"] = out
            return out

    @app.post("/api/slack/channels/changed")
    def slack_channels_changed(body: SlackChannelsChangedIn | None = None):
        """Tares Cloud forwards Slack's channel events here (member_joined_channel, channel_created,
        channel_rename, channel_archive); the next read of the list asks Slack again."""
        _drop_channels_cache()
        return {"ok": True}

    # ── the Slack signing secret: the credential that makes inbound Slack safe ──
    # Same write-only contract as the bot token. Without it POST /api/slack/events answers 503:
    # there is no configuration in which accepting an unverified inbound request is correct.
    @app.get("/api/settings/slack-signing-secret")
    def get_slack_signing_secret():
        secret, origin = slack_verify.resolve_secret(store)
        return {"configured": bool(secret), "source": origin,
                "stored": bool(store.get_setting(slack_verify.SETTING_KEY)),
                "env_overrides": origin.startswith("env:")}

    @app.put("/api/settings/slack-signing-secret")
    def set_slack_signing_secret(body: SlackSigningSecretIn):
        secret = body.secret.strip()
        if not secret:
            _err(ValueError("secret is required (use DELETE to remove the stored secret)"))
        if secret.startswith("xox"):
            # A bot token pasted into the wrong box would look configured and fail every signature.
            _err(ValueError("that is a bot token, not the signing secret; take the Signing Secret "
                            "from the Slack app's Basic Information page"))
        store.set_setting(slack_verify.SETTING_KEY, secret)
        _, origin = slack_verify.resolve_secret(store)
        return {"ok": True, "source": origin,
                **({"note": "an environment secret takes precedence and is still in use"}
                   if origin.startswith("env:") else {})}

    @app.delete("/api/settings/slack-signing-secret")
    def clear_slack_signing_secret():
        store.set_setting(slack_verify.SETTING_KEY, None)
        secret, origin = slack_verify.resolve_secret(store)
        return {"ok": True, "configured": bool(secret), "source": origin}

    # ── inbound Slack: the /tares slash command ───────────────────────────
    # The only route in this daemon that is public to the auth middleware AND accepts a body from
    # the internet, so it carries its own authentication: an HMAC over the raw bytes.
    _slack_workspace = slack_mod.workspace_slug(SLACK_CONNECT_URL)   # None self-hosted
    _ask_tasks: set = set()      # keeps background tasks referenced; asyncio only holds weak refs

    async def _slack_respond(response_url: str, body: dict) -> None:
        """Deliver one message to Slack's `response_url`. Best-effort with a couple of retries: the
        user is waiting, and there is nowhere else to put the answer if this fails."""
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10) as cx:
                    r = await cx.post(response_url, json=body)
                if r.status_code < 400:
                    return
                if r.status_code < 500:
                    print(f"taresd: slack response_url rejected the answer: "
                          f"HTTP {r.status_code} {r.text[:200]}", flush=True)
                    return
            except Exception as e:
                print(f"taresd: slack response_url failed: {type(e).__name__}: {e}", flush=True)
            if attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))

    async def _slack_post(token: str, method: str, body: dict) -> None:
        try:
            async with httpx.AsyncClient(timeout=15) as cx:
                r = await cx.post(f"{slack_mod.API_BASE}/{method}", json=body,
                                  headers={"authorization": f"Bearer {token}"})
            try:
                data = r.json()
            except Exception:
                data = None
            ok, error, _retry = slack_mod.classify(r.status_code, data)
            if not ok:
                print(f"taresd: slack {method}: {error}", flush=True)
        except Exception as e:
            print(f"taresd: slack {method} failed: {type(e).__name__}: {e}", flush=True)

    async def _slack_interaction(payload: dict) -> None:
        """A click on a button Tares posted that is not a link: an old message's button whose
        feature is gone. Say so to the clicker alone, through the payload's response_url. Never
        raises: it runs detached from the request."""
        try:
            response_url = str(payload.get("response_url") or "").strip()
            if response_url:
                await _slack_respond(response_url, {"response_type": "ephemeral",
                                                    "replace_original": False,
                                                    "text": slack_mod.GONE_NOTE})
        except Exception as e:   # noqa: BLE001 - nobody is waiting on this, so the log is the place
            print(f"taresd: slack interaction failed: {type(e).__name__}: {e}", flush=True)

    @app.post(SLACK_EVENTS_PATH, include_in_schema=False)
    async def slack_events(request: Request):
        """Slack's inbound endpoint: the URL-verification handshake, button clicks, and the
        `/tares` command, which only says that asking from Slack is gone (TR-1810).

        Two contracts shape this whole handler:

        · **The signature covers the RAW body.** Read `await request.body()` and parse afterwards —
          letting FastAPI decode and re-serialise changes the bytes and every HMAC fails.
        · **Slack allows 3 seconds to ACK.** Anything slower (the note to a button's clicker)
          runs in a background task after the ACK.
        """
        raw = await request.body()
        secret, _origin = slack_verify.resolve_secret(store)
        if not secret:
            # Never "accept and hope". Refusing to serve is the only safe failure here.
            return JSONResponse({"detail": "Slack is not configured on this instance: set "
                                           f"{slack_verify.ENV_VAR} (or add the signing secret "
                                           "under Settings) and try again"}, status_code=503)
        if (why := slack_verify.check(request.headers, raw, secret)) is not None:
            # The reason goes to the operator's log, not to the caller: telling a forger which part
            # of their forgery failed is free help.
            print(f"taresd: rejected an inbound Slack request ({why})", flush=True)
            return JSONResponse({"detail": "invalid Slack signature"}, status_code=401)

        ctype = request.headers.get("content-type", "")
        if "json" in ctype:
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                return JSONResponse({"detail": "malformed JSON"}, status_code=400)
            if body.get("type") == "url_verification":
                # Without this the Slack app can never be pointed at this daemon at all.
                return {"challenge": body.get("challenge", "")}
            # Event subscriptions are out of scope (slash command only) — ACK so Slack doesn't
            # retry, and say nothing.
            return {"ok": True}

        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        if "payload" in form:
            # An interaction: someone clicked a button on a message Tares posted. A link button
            # (Open in Tares on a waiting item, View in Tares) already took the browser where it
            # points, so its click only needs the 200. Any other button is an old message's, whose
            # feature is gone: the clicker is told so, in the background. Slack wants its 200
            # within 3 seconds, and without it puts a warning triangle next to the button. Always
            # 200, whatever the payload holds.
            try:
                payload = json.loads(form["payload"])
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("type") == "block_actions" \
                    and not slack_mod.is_link_click(payload) \
                    and not slack_mod.is_foreign_click(payload, _slack_workspace):
                task = asyncio.create_task(_slack_interaction(payload))
                _ask_tasks.add(task)
                task.add_done_callback(_ask_tasks.discard)
            elif not (isinstance(payload, dict) and payload.get("type") == "block_actions"):
                print("taresd: ignored a Slack interaction that is not a block action", flush=True)
            return Response(status_code=200)
        # `/tares ask` went with Ask (TR-1810): any command gets the same note, ephemeral
        thread_ts = (form.get("thread_ts") or "").strip() or None
        return slack_mod.build_error(slack_mod.ASK_GONE_NOTE, thread_ts)

    # ── management API: projects (tares/factory_projects.py) ─────────────────
    def _uc_err(e: Exception):
        if isinstance(e, KeyError):
            _err(e, 404)
        if isinstance(e, (ProjectError, ValueError)):
            _err(e, 400)
        raise e

    def _project_card(uid: str) -> dict:
        """What a project key is told about its project: who it is and what it is made of, not
        its template parameters (they can hold credentials)."""
        p = store.get_project(uid) or {}
        return {"id": uid, "name": p.get("name"), "template": p.get("template"),
                "goal": p.get("goal"),
                "status": p.get("status"), "created_at": p.get("created_at"),
                "skills": [s["name"] for s in store.list_skills(uid)]}

    # Licence-independent by design (TR-915): never consult licence or team here.
    def _hidden_project(p: dict) -> dict:
        """A private project as someone not on it sees it: enough to name it and to hand its
        lead over, nothing of what is in it."""
        return {"id": p["id"], "name": p["name"], "scope": "private", "hidden": True,
                "lead": p["lead"]}

    @app.get("/api/projects")
    @request_memo.page
    def list_projects(request: Request):
        pk = _pk(request)
        if pk:
            return {"projects": [_project_card(pk)]}
        # a software factory project carries its tickets' progress, for the list's ring, and
        # `activity_at`, when it last moved (its newest ticket counts), for the Updated column;
        # every factory project's tickets come in one query (TR-659)
        # a private project the caller is not on is listed as its name and lead and nothing else
        # (an admin hands its lead over from this row); everything else about it is unknown
        cards = projects.list()
        tickets = store.tickets_by_project([p["id"] for p in cards
                                            if p.get("kind") == "software_factory"
                                            and _can_see(p)])

        def card(p: dict) -> dict:
            if not _can_see(p):
                return _hidden_project(p)
            if p.get("kind") != "software_factory":
                return {**p, "activity_at": p["updated_at"], "ready_to_close": False}
            ts = tickets[p["id"]]
            return {**p, "progress": factory_mod.progress(ts),
                    "activity_at": factory_mod.activity_at(p["updated_at"], ts),
                    "ready_to_close": factory_mod.ready_to_close(p, ts)}
        return {"projects": [card(p) for p in cards]}

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn, request: Request):
        # the person who makes a project leads it; the system credential is no person, so a
        # project it makes has no lead until an admin claims it
        person = _current_person(request)
        try:
            return projects.create(body.name, goal=body.goal, icon=body.icon, kind=body.kind,
                                   lead_person_id=person["id"] if person else None)
        except Exception as e:
            _uc_err(e)

    @app.get("/api/projects/{uid}")
    def get_project(uid: str, request: Request):
        if _pk(request):
            return _project_card(uid)   # the guard already held it to the key's own project
        _project_or_404(uid)
        inst = projects.get(uid)
        if inst is None:
            _err(KeyError(f"unknown project {uid!r}"), 404)
        return inst

    @app.put("/api/projects/{uid}")
    def update_project(uid: str, body: ProjectUpdate):
        """Change the name, the goal or the icon; what the body leaves out stays as it is."""
        _project_or_404(uid)
        given = body.model_fields_set
        kw = {k: getattr(body, k) for k in ("goal", "icon") if k in given}
        try:
            out = projects.update(uid, name=body.name, **kw)
        except Exception as e:
            _uc_err(e)
        if body.name.strip() or "icon" in given:
            factory_home.clear_cache()   # Home keeps its answer for a few seconds
        return out

    @app.delete("/api/projects/{uid}")
    def delete_project(uid: str):
        _project_or_404(uid)
        try:
            out = projects.delete(uid)
        except Exception as e:
            _uc_err(e)
        factory_home.clear_cache()
        return out

    @app.post("/api/projects/{uid}/pause")
    def pause_project(uid: str):
        _project_or_404(uid)
        try:
            return projects.pause(uid)
        except Exception as e:
            _uc_err(e)

    @app.post("/api/projects/{uid}/resume")
    def resume_project(uid: str):
        _project_or_404(uid)
        try:
            return projects.resume(uid)
        except Exception as e:
            _uc_err(e)

    @app.get("/api/projects/{uid}/timeline")
    async def project_timeline(request: Request, uid: str, limit: int = 50, before: str = "",
                               trigger: str = "", agent: str = "", outcome: str = "",
                               entity: str = ""):
        """What happened in the project, newest first (TR-331): the hand-overs pushed through
        without every sign-off (`events`). `threads` stays, empty, since triggers and agents went.
        Page with `before` = the previous page's `next_before`; a `before` without a zone is UTC.
        Any of `trigger`, `agent`, `outcome`, `entity` filters threads, so `events` are left out."""
        _project_or_404(uid)
        if projects.get(uid) is None:
            _err(KeyError(f"unknown project {uid!r}"), 404)
        at = None
        if before:
            try:
                at = datetime.fromisoformat(before.replace("Z", "+00:00"))
                if at.tzinfo is None:   # no zone means UTC, not the host's time
                    at = at.replace(tzinfo=timezone.utc)
            except ValueError:
                _err(ValueError("before must be an ISO timestamp, as next_before gives it"))
        if outcome and outcome not in timeline.OUTCOMES:
            _err(ValueError(f"outcome must be one of {', '.join(timeline.OUTCOMES)}"))
        out = await asyncio.to_thread(
            timeline.project_timeline, store, uid, limit=limit, before=at, trigger=trigger,
            agent=agent, outcome=outcome, entity=entity, scheduled=set())
        filtered = bool(trigger or agent or outcome or entity)
        events = [] if filtered else timeline.override_events(
            store.list_handover_overrides(uid), at, out.get("next_before"))
        if events:
            # every hand-over the lead pushed through without every sign-off (TR-636), on the page
            # whose window holds its time, so across pages none repeats and none is lost
            out["events"] = events
        return out

    @app.get("/api/projects/{uid}/summary")
    def project_summary(uid: str):
        _project_or_404(uid)
        try:
            out = projects.summary(uid)
        except Exception as e:
            _uc_err(e)
        factory = out.get("kind") == "software_factory"
        out["ready_to_close"] = factory and factory_mod.ready_to_close(out, store.list_tickets(uid))
        return out

    # ── model usage: one ledger row per model call the cell makes ────────────
    def _record_model_usage(model: str, usage: dict, key_source: str = "", kind: str = "anthropic",
                            provider_id: str | None = None) -> None:
        """One ledger row per summary (the standup, a milestone, the report), so the
        cell's spend meter covers everything that talks to a model. `key_source` is the origin of
        the key that PAID for it, captured by the caller when it resolved the key, never
        re-resolved here: the stored key could have changed meanwhile. The surface stays "ask",
        the name these calls had in the ledger before agents went (TR-1810)."""
        from .pricing import price_usage
        store.record_model_usage(
            "ask", "", "", model, usage["calls"],
            usage["input_tokens"], usage["output_tokens"],
            usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"],
            price_usage(kind, model, usage),
            key_source=key_source or None, provider=provider_id)

    # ── a project's docs and tickets (TR-403): what a spec session leaves for the session that
    # builds it. Docs belong to the cell and projects include them; tickets are owned by Linear
    # when the project uses it (written by the sync only), by Tares otherwise ────────────────
    def _by(request: Request, body: dict | None = None) -> str:
        """Who made a change: what the caller says (`by`), else the person behind its credential,
        else the caller's own default label (`by_default`, which the Claude Code tools send and
        which never beats a person), else the credential's own name."""
        said = str((body or {}).get("by") or "").strip()[:100]
        default = str((body or {}).get("by_default") or "").strip()[:100]
        cred = getattr(request.state, "credential", None) or {}
        person = _current_person(request) or {}
        return said or person.get("name") or default or cred.get("name") or ""

    # The cell's global docs (its AGENTS.md and memory): made the first time a project has docs,
    # included in every project that has docs, so every session working on a project reads them.
    def _global_ids(create: bool = False) -> dict[str, str]:
        """{kind: doc id} of the global docs that exist; with `create`, make the missing ones."""
        return memory_tares.global_ids(store, create)

    def _ensure_globals(uid: str) -> dict[str, str]:
        """A project with docs includes the global docs (made now if this is the first one)."""
        mine = store.list_docs(uid)
        if not mine:
            return _global_ids()
        ids = _global_ids(create=True)
        have = {d["id"] for d in mine}
        for doc_id in ids.values():
            if doc_id not in have:
                store.use_doc(uid, doc_id)
        return ids

    def _marked(d: dict, ids: dict[str, str]) -> dict:
        return {**d, "global": d["id"] in ids.values()}

    def _doc_or_404(uid: str, doc_id: str) -> dict:
        d = store.get_doc(uid, doc_id)
        if d is None:
            _err(KeyError(f"project has no doc {doc_id!r}"), 404)
        return _marked(d, _global_ids())

    @app.get("/api/projects/{uid}/docs")
    def list_project_docs(uid: str, kind: str = ""):
        """The docs the project includes, without bodies, in reading order (starting prompt,
        spec, plan, AGENTS.md, memory, notes, working docs). The cell's global AGENTS.md and
        memory are among them (`global`) once the project has any doc."""
        _project_or_404(uid)
        ids = _ensure_globals(uid)
        return [_marked(d, ids)
                for d in docs_mod.sort_docs(store.list_docs(uid, kind.strip().lower() or None))]

    @app.get("/api/docs/global")
    def get_global_docs(request: Request):
        """The cell's global AGENTS.md and memory, with their bodies; null until the first
        project has docs. `person` and `personal` are the caller's own, see /api/docs/personal."""
        ids = _global_ids()
        out = {k: (_marked(store.get_doc(None, ids[k]), ids) if k in ids else None)
               for k in docs_mod.GLOBAL_DOCS}
        # the caller's own AGENTS.md and memory ride along: only theirs, never another person's
        out["person"], out["personal"] = _personal_out(request)
        return out

    def _personal_out(request: Request) -> tuple[dict | None, dict]:
        """(the caller's person, {agents, memory}: their personal docs with bodies or null)."""
        person = _current_person(request)
        mine = {name: (store.personal_doc(person["id"], kind) if person else None)
                for name, (kind, _title) in docs_mod.PERSONAL_DOCS.items()}
        return ({"id": person["id"], "name": person["name"]} if person else None), mine

    @app.get("/api/docs/personal")
    def get_personal_docs(request: Request):
        """The caller's own personal AGENTS.md and memory, with their bodies (null until the first
        line). Another person's are not reachable by any route."""
        person, mine = _personal_out(request)
        return {"person": person, **mine}

    @app.post("/api/docs/personal/{name}/lines")
    def add_personal_line(name: str, request: Request, body: dict = Body(...)):
        """{text}: add one line to the caller's personal AGENTS.md (`agents`) or memory
        (`memory`), made with the first line. Any station of that person may. The person comes
        from the credential: a key with no person has none to write for."""
        if name not in docs_mod.PERSONAL_DOCS:
            _err(KeyError(f"no personal doc {name!r}; one of: " + ", ".join(docs_mod.PERSONAL_DOCS)),
                 404)
        person = _current_person(request)
        if not person:
            _err(PermissionError("This key has no person, so it has no personal docs."), 403)
        kind, title = docs_mod.PERSONAL_DOCS[name]
        cur = store.personal_doc(person["id"], kind)
        try:
            text, added = docs_mod.append_line(cur["body"] if cur else "", body.get("text"),
                                           limit=docs_mod.MAX_PERSONAL_BYTES)
        except docs_mod.DocError as e:
            _err(e)
        by = _by(request, body)
        if cur is None:
            doc_id = store.create_doc(None, kind, title, text, by=by, owner=person["id"])
        else:
            doc_id = cur["id"]
            if added:
                store.update_doc(doc_id, body=text, by=by)
        return {**store.personal_doc(person["id"], kind), "added": added}

    @app.post("/api/docs/global/{kind}/lines")
    async def add_global_line(kind: str, request: Request, body: dict = Body(...)):
        """{text}: add one line to the global AGENTS.md (`agents`) or memory (`memory`). A line
        already there is not added twice (`added`: false). A memory line goes to the Tares doc
        first, then to the memory integration the cell uses; `pending` is true when that one
        did not take it and it waits for a retry."""
        if kind == "grants":
            _err(ValueError("a grant quotes the person and carries its date: add it with "
                            "add_grant (POST /api/grants)"))
        if kind not in docs_mod.GLOBAL_DOCS:
            _err(KeyError(f"no global doc {kind!r}; one of: " + ", ".join(docs_mod.GLOBAL_DOCS)),
                 404)
        why = factory_mod.may(factory_api.caller(request), "global_docs")
        if why:
            _err(PermissionError(why), 403)
        person = _current_person(request)
        # the name sits in a shared doc: one line, so a name cannot add a bullet of its own
        who = " ".join(str((person or {}).get("name") or "").split())[:60].strip()
        note = (f" (added by {who}, {datetime.now(timezone.utc).date().isoformat()})"
                if who else "")
        if kind == "memory":
            try:
                res = await memory_route.write(store, memory_tares.FACTORY, body.get("text"), note,
                                               _by(request, body))
            except docs_mod.DocError as e:
                _err(e)
            ids = _global_ids()
            return {**_marked(store.get_doc(None, res["doc_id"]), ids), "added": res["added"],
                    "pending": res["pending"]}
        ids = _global_ids(create=True)
        cur = store.get_doc(None, ids[kind])
        try:
            text, added = docs_mod.append_line(cur["body"], body.get("text"), note)
        except docs_mod.DocError as e:
            _err(e)
        if added:
            store.update_doc(ids[kind], body=text, by=_by(request, body))
        return {**_marked(store.get_doc(None, ids[kind]), ids), "added": added}

    @app.post("/api/projects/{uid}/docs", status_code=201)
    def create_project_doc(uid: str, request: Request, body: dict = Body(...)):
        """{kind, title, body}: a new doc in the project."""
        _project_or_404(uid)
        try:
            kind, title, text = docs_mod.validate_doc(body.get("kind"), body.get("title"),
                                                      body.get("body"))
        except docs_mod.DocError as e:
            _err(e)
        if kind == "grants":
            _err(ValueError("a grant quotes the person and carries its date: add it with "
                            "add_grant (POST /api/grants)"))
        doc_id = store.create_doc(uid, kind, title, text, by=_by(request, body))
        _ensure_globals(uid)
        return _doc_or_404(uid, doc_id)

    @app.get("/api/projects/{uid}/docs/{doc_id}")
    def get_project_doc(uid: str, doc_id: str):
        _project_or_404(uid)
        return _doc_or_404(uid, doc_id)

    @app.put("/api/projects/{uid}/docs/{doc_id}")
    def update_project_doc(uid: str, doc_id: str, request: Request,
                                 body: dict = Body(...)):
        """Change the kind, title or body; a field left out keeps its value. Docs are shared: the
        change shows in every project that includes the doc."""
        _project_or_404(uid)
        cur = _doc_or_404(uid, doc_id)
        try:
            kind, title, text = docs_mod.validate_doc(body.get("kind", cur["kind"]),
                                                      body.get("title", cur["title"]),
                                                      body.get("body", cur["body"]))
        except docs_mod.DocError as e:
            _err(e)
        if "grants" in (kind, cur["kind"]) and (kind, title, text) != (cur["kind"], cur["title"],
                                                                         cur["body"]):
            _err(ValueError("grants change only through add_grant (POST /api/grants), which "
                            "quotes the person and dates it"))
        if cur["global"]:
            why = factory_mod.may(factory_api.caller(request), "global_docs")
            if why:
                _err(PermissionError(why), 403)
        store.update_doc(doc_id, kind=kind, title=title, body=text, by=_by(request, body))
        return _doc_or_404(uid, doc_id)

    @app.delete("/api/projects/{uid}/docs/{doc_id}")
    def delete_project_doc(uid: str, doc_id: str):
        """The project stops including the doc; it is deleted when no other project includes
        it."""
        _project_or_404(uid)
        d = _doc_or_404(uid, doc_id)
        if d["global"]:
            _err(ValueError(f"{d['title']} belongs to every project; edit it instead"), 409)
        others = [p for p in d["projects"] if p != uid]
        store.remove_doc(uid, doc_id)
        return {"ok": True, "removed": doc_id, "kept_for": others}

    @app.post("/api/projects/{uid}/docs/{doc_id}/use")
    def use_project_doc(uid: str, doc_id: str):
        """The project includes a doc already on the cell (shared: an edit shows everywhere)."""
        _project_or_404(uid)
        if _owned_by_hidden("doc", doc_id, _hidden_ids()) or not store.use_doc(uid, doc_id):
            _err(KeyError(f"there is no doc {doc_id!r} on Tares"), 404)
        return _doc_or_404(uid, doc_id)

    @app.get("/api/docs")
    def list_cell_docs(request: Request):
        """Every doc on Tares, with the projects that include it (no bodies). A person's own
        AGENTS.md and memory show only to them."""
        viewer = (_current_person(request) or {}).get("id")
        names = {p["id"]: p["name"] for p in store.list_projects()}
        ids = _global_ids()
        hidden = _hidden_ids()
        return [{**_marked(d, ids),
                 "projects": [{"id": p, "name": names.get(p, p)} for p in d["projects"]
                              if p not in hidden]}
                for d in docs_mod.sort_docs(store.list_cell_docs(viewer))
                if not d["projects"] or set(d["projects"]) - hidden]

    def _plan_ctx(uid: str, listing: bool = False) -> dict:
        """What every ticket's view needs from the rest of the plan: the tickets by id, the
        milestones' names, and which tickets are ready (TR-411, TR-412). `listing`: also every
        working doc's title and each staged ticket's last review, fetched once for the whole
        list instead of per ticket (TR-657)."""
        tickets = store.list_tickets(uid)
        ctx = {"by_id": {t["id"]: t for t in tickets},
               "ms": {m["id"]: m["name"] for m in store.list_milestones(uid)},
               "ready": factory_mod.readiness(tickets)}
        if listing:
            ctx["titles"] = store.doc_titles(uid, sorted({t["working_doc"] for t in tickets
                                                          if t["working_doc"]}))
            ctx["last_review"] = store.last_reviews([t["id"] for t in tickets if t["stage"]])
        return ctx

    def _assignee_view(t: dict) -> dict:
        """The ticket's `assignee` ({id, name} or None) and `assignee_raw` (set only while nobody
        on the cell is the assignee). A read of what is stored: it shows whatever the licence."""
        person = store.get_person(t["assignee_person_id"]) if t.get("assignee_person_id") else None
        if person:
            return {"assignee": {"id": person["id"], "name": person["name"]}, "assignee_raw": None}
        return {"assignee": None, "assignee_raw": t.get("assignee_raw")}

    def _ticket_out(uid: str, t: dict, with_doc: bool = False, ctx: dict | None = None) -> dict:
        ctx = ctx or _plan_ctx(uid)
        out = {k: t[k] for k in ("id", "owner", "identifier", "url", "title", "status",
                                 "position", "working_doc", "updated_at", "number", "milestone",
                                 "holder", "stage", "stage_reason", "pr", "repo")}
        out.update(_assignee_view(t))   # assignee {id, name} | None, assignee_raw
        out["label"] = factory_mod.label(t)
        out["pr_note"] = factory_api.pr_note(t)
        out["milestone_name"] = ctx["ms"].get(t["milestone"]) if t["milestone"] else None
        out["depends_on"] = [factory_mod.label(ctx["by_id"][d]) for d in t["depends_on"]
                             if d in ctx["by_id"]]
        r = ctx["ready"].get(t["id"]) or {"ready": False, "blocked_by": []}
        out["ready"], out["blocked_by"] = r["ready"], r["blocked_by"]
        if "titles" in ctx:
            out["working_doc_title"] = ctx["titles"].get(t["working_doc"])
        else:
            doc = store.get_doc(uid, t["working_doc"]) if t["working_doc"] else None
            out["working_doc_title"] = doc["title"] if doc else None
        if with_doc:
            out["working_doc_body"] = doc["body"] if doc else None
            out["history"] = store.ticket_history(t["id"])
            by_id = ctx["by_id"]
            out["assumptions"] = [
                {**{k: a[k] for k in ("label", "question", "choice", "why", "state", "words",
                                      "made_by", "person", "by_label")},
                 "tickets": [factory_mod.label(by_id[x]) for x in a["tickets"] if x in by_id]}
                for a in store.list_assumptions(uid, ticket=t["id"])]
            out.update(factory_api.review_state(h, uid, t, _cc_source()))
            lab = out["label"]
            ds = store.list_decisions(uid, all_=True)
            out["decisions"] = ([d for d in ds if lab in d["tickets"]]
                                + [d for d in ds if lab not in d["tickets"]])[:10]
        elif "last_review" in ctx:
            out["verdict"] = factory_api.verdict_now(t, ctx["last_review"].get(t["id"]))
        else:
            rounds = store.ticket_reviews(t["id"]) if t["stage"] else []
            out["verdict"] = factory_api.verdict_now(t, factory_mod.standing_round(rounds))
        return out

    def _tickets_out(uid: str) -> list[dict]:
        ctx = _plan_ctx(uid, listing=True)
        return [_ticket_out(uid, t, ctx=ctx) for t in ctx["by_id"].values()]

    def _milestone_ref(uid: str, ref) -> str | None:
        """The id of the milestone `ref` names (id or name), None for "" or None; 404 otherwise."""
        ref = str(ref or "").strip()
        if not ref:
            return None
        m = store.get_milestone(uid, ref)
        if m is None:
            names = [x["name"] for x in store.list_milestones(uid)]
            _err(KeyError(f"project has no milestone {ref!r}"
                          + ("; one of: " + ", ".join(names) if names
                             else "; write it first with write_milestone")), 404)
        return m["id"]

    def _depends_ref(uid: str, tid: str | None, refs) -> list[str]:
        """The ids of the tickets `refs` name, refusing an unknown ticket, the ticket itself and
        a dependency loop."""
        try:
            refs = factory_mod.depends_ok(refs)
        except docs_mod.DocError as e:
            _err(e)
        ids: list[str] = []
        for ref in refs:
            d = store.get_ticket(uid, ref)
            if d is None:
                _err(KeyError(f"project has no ticket {ref!r} to depend on"), 404)
            if d["id"] == tid:
                _err(ValueError("a ticket cannot depend on itself"))
            if d["id"] not in ids:
                ids.append(d["id"])
        if tid and ids:
            deps = {t["id"]: t["depends_on"] for t in store.list_tickets(uid)}
            deps[tid] = ids
            loop = factory_mod.find_loop(deps, tid)
            if loop:
                by_id = {t["id"]: t for t in store.list_tickets(uid)}
                _err(ValueError("that makes a dependency loop: " + " -> ".join(
                    factory_mod.label(by_id[x]) for x in loop if x in by_id)), 409)
        return ids

    def _repo_ok(v) -> str | None:
        try:
            return factory_mod.repo_ok(v)
        except docs_mod.DocError as e:
            _err(e)

    def _home_title(owner: str) -> str:
        """Where a ticket or milestone Tares does not own lives, in a word: Linear, GitHub."""
        cls = integrations_registry.get("tickets", owner)
        return (getattr(cls, "title", None) or owner).split()[0]

    def _assignee_ref(v) -> tuple[str | None, str | None]:
        """(person id, raw) for the `assignee` a caller sent; 404 for text that is neither a
        person id nor an email."""
        try:
            return team_mod.get().assignee_resolve(store, v)
        except ValueError as e:
            _err(KeyError(str(e)), 404)

    def _assign_join(request, project: dict, prev: str | None, who: str | None) -> bool:
        """May this caller give the ticket `who` as its assignee, and does that add them to the
        project? Only a change joins anyone, and on a private project only the lead or the system
        credential may bring in someone who is not on it (403 otherwise, like the members route);
        any member may assign a person already on it."""
        if (not who or who == prev or project.get("scope") != "private"
                or who == project.get("lead_person_id")
                or store.is_project_member(project["id"], who)):
            return False
        me = _current_person(request)
        if not _is_system(request) and not (me and me["id"] == project.get("lead_person_id")):
            raise HTTPException(status_code=403, detail=(
                "that person is not on this private project: assign someone who is, or ask the "
                "project lead to add them"))
        return True

    def _added_by(request) -> str:
        """Who a project member added by assigning a ticket is recorded as: the person behind the
        credential, from the credential alone."""
        by = _current_person(request)
        return by["id"] if by else "system"

    def _ticket_or_404(uid: str, ref: str) -> dict:
        t = store.get_ticket(uid, ref)
        if t is None:
            _err(KeyError(f"project has no ticket {ref!r}"), 404)
        return t

    def _working_doc_ok(uid: str, doc_id) -> str | None:
        if not doc_id:
            return None
        if store.get_doc(uid, str(doc_id)) is None:
            _err(KeyError(f"project has no doc {doc_id!r}; write the working doc first"), 404)
        return str(doc_id)

    @app.get("/api/projects/{uid}/tickets")
    async def list_project_tickets(uid: str):
        """The project's tickets in order, each with the title of its working doc, and the
        Linear project they live in when the project uses Linear (`linear`, with the last
        sync's time and error), or the GitHub repo and issue label when it uses GitHub Issues
        (`github`, the same)."""
        await asyncio.to_thread(_project_or_404, uid)
        # no GitHub call (TR-1474): the pull request state kept so far, which the background sync
        # and a ticket's own read keep current; the store work runs in a thread, so other
        # requests are served meanwhile
        return await asyncio.to_thread(lambda: {
            "tickets": _tickets_out(uid), "linear": store.get_project_linear(uid),
            "github": store.get_project_github(uid)})

    @app.post("/api/projects/{uid}/tickets", status_code=201)
    async def create_project_ticket(uid: str, request: Request, body: dict = Body(...)):
        """{title, status?, position?, working_doc?, milestone?, depends_on?, repo?, assignee?}: a
        ticket Tares owns. `repo` is the GitHub repo (owner/name) it changes, for a project that
        spans several. `assignee` is a person id or an email (an email nobody on the cell has is
        kept as written, and the ticket shows as unowned). When the project's tickets live in
        Linear or GitHub the ticket is created there, through the tickets integration, and Tares
        keeps the one row for it (TR-851): it takes title, body, milestone (a milestone already
        written), depends_on (tickets already written), working_doc and repo; its status, assignee
        and position come from Linear or GitHub (409). A repeat of the same title finds the issue
        again."""
        project = await asyncio.to_thread(_project_or_404, uid)
        home = await asyncio.to_thread(store.project_ticket_home, uid)
        if home:
            return await _create_foreign_ticket(uid, home, body)
        return await asyncio.to_thread(_create_tares_ticket, uid, project, request, body)

    def _create_tares_ticket(uid: str, project: dict, request: Request, body: dict) -> dict:
        try:
            title = docs_mod.title_ok(body.get("title"))
            status = docs_mod.status_ok(body.get("status"))
        except docs_mod.DocError as e:
            _err(e)
        who, raw = _assignee_ref(body.get("assignee"))
        joins = _assign_join(request, project, None, who)
        pos = body.get("position")
        milestone = _milestone_ref(uid, body.get("milestone"))
        depends = _depends_ref(uid, None, body.get("depends_on"))
        tid = store.create_ticket(uid, "tares", title, status,
                                  float(pos) if pos is not None else None,
                                  working_doc=_working_doc_ok(uid, body.get("working_doc")),
                                  milestone=milestone, depends_on=depends,
                                  repo=_repo_ok(body.get("repo")),
                                  assignee_person_id=who, assignee_raw=raw)
        if joins:
            team_mod.get().assignee_join(store, project, who, _added_by(request))
        return _ticket_out(uid, _ticket_or_404(uid, tid))

    def _foreign_ticket_plan(uid: str, home: str, body: dict):
        """What creating a ticket in Linear or GitHub needs, checked before anything is made:
        (the integration class, the item for its `create`, the milestone's Tares id, the
        dependencies' Tares ids, repo, working doc). Every 4xx is raised here."""
        held = sorted({"status", "assignee", "position"} & set(body))
        if held:
            _err(ValueError(f"this project's tickets live in {_home_title(home)}: a ticket's "
                            + ", ".join(held) + " changes in " + _home_title(home)
                            + ", not when it is created"), 409)
        cls = integrations_registry.get("tickets", home)
        if cls is None:
            _err(ValueError(f"this project's tickets live in {_home_title(home)}; add the ticket "
                            "there and it shows up here on the next sync"), 409)
        try:
            title = docs_mod.title_ok(body.get("title"))
        except docs_mod.DocError as e:
            _err(e)
        mid = _milestone_ref(uid, body.get("milestone"))
        m = store.get_milestone(uid, mid) if mid else None
        deps = _depends_ref(uid, None, body.get("depends_on"))
        item = {"title": title, "body": str(body.get("body") or ""),
                "milestone": m["name"] if m else "", "depends_on": deps}
        return cls, item, mid, deps, _repo_ok(body.get("repo")), \
            _working_doc_ok(uid, body.get("working_doc"))

    async def _create_foreign_ticket(uid: str, home: str, body: dict) -> dict:
        cls, item, mid, deps, repo, doc = await asyncio.to_thread(_foreign_ticket_plan, uid,
                                                                  home, body)
        try:
            refs = await cls(store).create(uid, [], [item])
        except (_linear.LinearError, _gh_tickets.GitHubTicketsError) as e:
            (_linear_err if home == "linear" else _gh_err)(e)
        except (ValueError, KeyError) as e:
            _err(e)
        if len(refs) != 1:
            _err(ValueError(f"{_home_title(home)} did not answer with the ticket"), 502)
        ref = refs[0]

        def _adopt() -> dict:
            tid = store.adopt_ticket(uid, home, ref.id, item["title"], ref.status or "todo",
                                     ref.identifier, ref.url, milestone=mid, depends_on=deps,
                                     repo=repo, working_doc=doc)
            return _ticket_out(uid, _ticket_or_404(uid, tid))
        return await asyncio.to_thread(_adopt)

    @app.get("/api/projects/{uid}/tickets/{ref}")
    async def get_project_ticket(uid: str, ref: str):
        """One ticket by its id, number or Linear identifier, with its working doc's body and
        its history (who moved it, when)."""
        _project_or_404(uid)
        t = _ticket_or_404(uid, ref)
        if await factory_api.refresh_pr(store, uid, t):   # its own pull request only (TR-1474)
            t = _ticket_or_404(uid, ref)
        return _ticket_out(uid, t, with_doc=True)

    @app.put("/api/projects/{uid}/tickets/link")
    async def link_project_tickets(uid: str, body: dict = Body(...)):
        """{integration, link_fields?}: link the project's tickets to Linear or GitHub Issues
        through that integration's `link`, then read them once. `link_fields` are the non-secret
        fields the integration names: `project` for Linear; `repo` and `issue_label` for GitHub.
        A body that carries a key or a token is refused (400): the cell's own credentials read the
        system. 409 when the project's tickets already live somewhere else. Answers
        {integration, tickets, linear | github}, the link with the state of its first sync (a
        sync that failed is on the link; the sync button tries again). DELETE /api/projects/{uid}/linear (or /github) unlinks."""
        project = await asyncio.to_thread(_project_or_404, uid)
        name = body.get("integration")
        if not isinstance(name, str) or name not in ("linear", "github"):
            _err(ValueError("integration is 'linear' or 'github'"))
        extra = sorted(str(k) for k in set(body) - {"integration", "link_fields"})
        if extra:
            _err(ValueError("this takes integration and link_fields, not: " + ", ".join(extra)))
        cls = integrations_registry.get("tickets", name)
        if cls is None:
            _err(KeyError(f"no tickets integration {name!r}"), 404)
        try:
            link = await cls(store).link(project, body.get("link_fields") or {})
        except _tickets_integ.LinkConflict as e:
            _err(e, 409)
        except _linear.LinearError as e:
            _linear_err(e)
        except _gh_tickets.GitHubTicketsError as e:
            _err(e, 429 if e.rate_limited else 400)
        except (ValueError, KeyError) as e:
            _err(e)
        # no source is made: the sync runner keeps the tickets in step (TR-1806)
        try:
            await (_linear_sync_now if name == "linear" else _github_sync_now)(uid)
        except (_linear.LinearError, _gh_tickets.GitHubTicketsError):
            pass   # recorded on the link; the answer shows it
        return await asyncio.to_thread(lambda: {
            "integration": name, "tickets": _tickets_out(uid),
            name: store.get_project_linear(uid) if name == "linear" else store.get_project_github(uid)})

    _TICKET_EDIT_FIELDS = {"title", "status", "position", "milestone", "depends_on", "assignee",
                           "working_doc", "repo"}

    @app.put("/api/projects/{uid}/tickets/{ref}")
    async def update_project_ticket(uid: str, ref: str, request: Request,
                                    body: dict = Body(...)):
        """Change a ticket. A Tares ticket takes title, status, position, assignee, milestone,
        depends_on, working_doc and repo. A Linear or GitHub ticket takes title, position (its
        order) and milestone, written to where it lives through the tickets integration (a slow
        write carries on in the background and the answer says `edit_pending`), plus working_doc
        and repo; its status, assignee and blocking relations change where it lives (409). Nothing
        is saved when Linear or GitHub refuses the edit. `assignee` is a person id or an email; ""
        clears it. Changing it leaves the holder and stage alone."""
        project = await asyncio.to_thread(_project_or_404, uid)
        t = await asyncio.to_thread(_ticket_or_404, uid, ref)
        unknown = sorted(str(k) for k in set(body) - _TICKET_EDIT_FIELDS)
        if unknown:
            _err(ValueError("a ticket takes " + ", ".join(sorted(_TICKET_EDIT_FIELDS))
                            + ", not: " + ", ".join(unknown)))
        foreign = t["owner"] != "tares"
        if foreign:
            # GitHub orders issues by number, so only Linear has an order to write
            home_only = {"status", "assignee", "depends_on"}
            if t["owner"] == "github":
                home_only.add("position")
            held = sorted(home_only & set(body))
            if held:
                _err(ValueError(f"{t['identifier'] or t['id']} lives in {_home_title(t['owner'])}: "
                                "change its " + ", ".join(held) + " there"), 409)
        fields, remote, mirror, joins = await asyncio.to_thread(
            _ticket_edit_plan, uid, t, project, request, body, foreign)
        pending = False
        if remote:
            try:
                landed = await factory_api.edit_ticket(store, t, t["owner"], remote, mirror)
            except (_linear.LinearError, _gh_tickets.GitHubTicketsError) as e:
                (_linear_err if t["owner"] == "linear" else _gh_err)(e)
            except (ValueError, KeyError) as e:
                _err(e)
            pending = not landed

        def _save() -> dict:
            if fields:
                why = (f"marked {fields['status'].replace('_', ' ')} in Tares"
                       if "status" in fields else None)
                store.update_ticket(t["id"], why=why, **fields)
            if joins:
                team_mod.get().assignee_join(store, project, fields["assignee_person_id"],
                                             _added_by(request))
            return _ticket_out(uid, _ticket_or_404(uid, t["id"]), with_doc=True)
        out = await asyncio.to_thread(_save)
        if pending:
            out["edit_pending"] = True
        return out

    def _ticket_edit_plan(uid: str, t: dict, project: dict, request, body: dict, foreign: bool):
        """What a ticket edit changes: (columns saved in Tares now, fields for the integration's
        `update`, the columns that mirror them once it lands, whether the assignee joins the
        project). Every 4xx is raised here, before anything is written."""
        fields: dict = {}
        remote: dict = {}
        mirror: dict = {}
        joins = False
        if "working_doc" in body:
            fields["working_doc"] = _working_doc_ok(uid, body.get("working_doc"))
        if "repo" in body:
            fields["repo"] = _repo_ok(body.get("repo"))
        try:
            if "title" in body:
                title = docs_mod.title_ok(body["title"])
                (remote if foreign else fields)["title"] = title
                if foreign:
                    mirror["title"] = title
            if "status" in body:
                fields["status"] = docs_mod.status_ok(body["status"])
        except docs_mod.DocError as e:
            _err(e)
        if "position" in body and body["position"] is not None:
            try:
                pos = float(body["position"])
            except (TypeError, ValueError):
                _err(ValueError("position is a number"))
            if foreign:
                remote["order"] = pos
                mirror["position"] = pos
            else:
                fields["position"] = pos
        if "milestone" in body:
            mid = _milestone_ref(uid, body.get("milestone"))
            if foreign:
                m = store.get_milestone(uid, mid) if mid else None
                if mid and m is None:
                    _err(KeyError(f"project has no milestone {body.get('milestone')!r}"), 404)
                remote["milestone"] = m["name"] if m else None
                mirror["milestone"] = mid
            else:
                fields["milestone"] = mid
        if "depends_on" in body:
            fields["depends_on"] = _depends_ref(uid, t["id"], body.get("depends_on"))
        if "assignee" in body:
            who, raw = _assignee_ref(body.get("assignee"))
            joins = _assign_join(request, project, t.get("assignee_person_id"), who)
            fields.update(assignee_person_id=who, assignee_raw=raw)
        return fields, remote, mirror, joins

    @app.delete("/api/projects/{uid}/tickets/{ref}")
    def delete_project_ticket(uid: str, ref: str):
        """Delete a Tares ticket (its working doc stays). A Linear or GitHub ticket is removed
        where it lives."""
        _project_or_404(uid)
        t = _ticket_or_404(uid, ref)
        if t["owner"] != "tares":
            _err(ValueError(f"{t['identifier'] or t['id']} lives in {_home_title(t['owner'])}: "
                            "remove it there"), 409)
        store.delete_ticket(t["id"])
        return {"ok": True, "deleted": t["id"]}

    # ── milestones (TR-411): each ships something a person can check, its acceptance checks.
    # A Linear or GitHub project's milestones come from there; their checks stay Tares's ──────
    def _milestones_out(uid: str) -> list[dict]:
        tickets = _tickets_out(uid)
        raw = store.list_tickets(uid)
        ms = store.list_milestones(uid)
        results = store.milestone_check_results([m["id"] for m in ms])
        accepted = store.milestone_acceptances([m["id"] for m in ms])
        from . import factory_waiting as _fw
        has = _fw.crew_has(store, store.get_project(uid) or {"id": uid})
        out = []
        for m in ms:
            mine = [t for t in tickets if t["milestone"] == m["id"]]
            live = [t for t in mine if t["status"] != "canceled"]
            done = sum(1 for t in live if t["status"] == "done")
            res = results.get(m["id"]) or {}
            st = factory_mod.milestone_state(m, [t for t in raw if t["milestone"] == m["id"]], res,
                                             accepted.get(m["id"]), has)
            results_out = [{"n": i + 1, "for": c.get("for") or "crew",
                            "result": (res.get(i) or {}).get("result"), "note": (res.get(i) or {}).get("note"),
                            "by": (res.get(i) or {}).get("by_name"), "at": (res.get(i) or {}).get("at")}
                           for i, c in enumerate(m["checks"])]
            out.append({**{k: m[k] for k in ("id", "owner", "name", "goal", "checks", "position",
                                              "updated_at")},
                        # each check's number, who it is for and its latest result (TR-1017)
                        "check_results": results_out,
                        "tickets": len(live), "done": done,
                        "ready": [t["label"] for t in mine if t["ready"]],
                        "finished": bool(live) and done == len(live),
                        # TR-1017: accepted (checks passed, or the person accepted it), and the
                        # checks still to run: the crew's are yours to run and record_milestone_check
                        "accepted": st["accepted"], "needs_person": st["needs_person"],
                        "crew_to_run": [i + 1 for i in st["crew_unrun"]],
                        "crew_failed": [i + 1 for i in st["crew_failed"]],
                        "person_wrong": [i + 1 for i in st["person_wrong"]]})
        return out

    def _milestone_or_404(uid: str, ref: str) -> dict:
        m = store.get_milestone(uid, ref)
        if m is None:
            _err(KeyError(f"project has no milestone {ref!r}"), 404)
        return m

    @app.get("/api/projects/{uid}/milestones")
    def list_project_milestones(uid: str):
        """The project's milestones in order, each with its goal, acceptance checks, and how far
        its tickets are (count, done, the ready ones, finished)."""
        _project_or_404(uid)
        return {"milestones": _milestones_out(uid)}

    @app.post("/api/projects/{uid}/milestones/{ref}/checks/{n}")
    def record_milestone_check(uid: str, ref: str, n: str, request: Request, body: dict = Body(...)):
        """{result: pass|fail, note?}: one run of a milestone's acceptance check n (1-based)
        (TR-1017). A station of the crew records its own checks; a person records the checks
        marked for a person. A person's fail is "something's wrong": on a project whose tickets
        are Tares's, a ticket to fix it is opened in the milestone; otherwise the orchestrator
        sees the failed check in list_milestones and opens it where the tickets live."""
        _project_or_404(uid)
        m = _milestone_or_404(uid, ref)
        n = int(n) if str(n).isdigit() else 0
        if not 1 <= n <= len(m["checks"]):
            _err(KeyError(f"{m['name']} has {len(m['checks'])} checks; there is no check {n}"), 404)
        result = str((body or {}).get("result") or "").strip().lower()
        if result not in ("pass", "fail"):
            _err(ValueError("result is pass or fail"))
        note = " ".join(str((body or {}).get("note") or "").split())[:2000] or None
        check = m["checks"][n - 1]
        station = factory_api.caller(request)["station"]
        person = _current_person(request)
        if not station and check.get("for") != "person":
            _err(ValueError(f"check {n} of {m['name']} is the crew's to run and record"), 409)
        if station and check.get("for") == "person":
            _err(ValueError(f"check {n} of {m['name']} is for a person to try; it shows on their "
                            "waiting list"), 409)
        if result == "fail" and not station and not note:
            _err(ValueError("say in one sentence what is wrong"))
        store.record_milestone_check(uid, m["id"], n - 1, result, note,
                                     "crew" if station else "person", station or _by(request, body),
                                     None if station else (person or {}).get("id"), now_utc())
        opened = None
        if result == "fail" and not station and store.project_ticket_home(uid) is None:
            title = f"Fix: {check['check']}"[:docs_mod.MAX_TITLE]
            opened = store.create_ticket(uid, "tares", title, "todo", milestone=m["id"])
            store.add_ticket_history(opened, uid, "stage", "todo",
                                     f"{_by(request, body)} tried check {n} of {m['name']}: {note}",
                                     "", "person", now_utc(), (person or {}).get("id"))
        out = next(x for x in _milestones_out(uid) if x["id"] == m["id"])
        return {**out, "opened": factory_mod.label(store.get_ticket(uid, opened)) if opened else None}

    # ── the summary of an accepted milestone (TR-1645, milestone_summary.py) ─────────────
    summaries = milestone_summary.Summaries(store, _record_model_usage)

    def _summary_of(uid: str, ref: str) -> tuple[dict, bool]:
        """(the milestone, whether it is accepted), for a project the caller may see."""
        _project_or_404(uid)
        m = _milestone_or_404(uid, ref)
        return m, next((x["accepted"] for x in _milestones_out(uid) if x["id"] == m["id"]), False)

    @app.get("/api/projects/{uid}/milestones/{ref}/summary")
    def read_milestone_summary(uid: str, ref: str):
        """{milestone, summary: {text, source, figures, written_at} | null, writing}: a few lines
        on what an accepted milestone delivered, the decisions taken on the way and what is left.
        Written once, when it is accepted or else on the first read, in the background: `writing`
        says to ask again shortly. A milestone not accepted has none."""
        m, accepted = _summary_of(uid, ref)
        if not accepted:
            return summaries.answer(m["id"], None, False)
        return summaries.read(uid, m["id"])

    @app.post("/api/projects/{uid}/milestones/{ref}/summary/rewrite", status_code=202)
    def rewrite_milestone_summary(uid: str, ref: str, request: Request):
        """Write an accepted milestone's summary again, from today's figures. Answers at once with
        the kept text and `writing`."""
        m, accepted = _summary_of(uid, ref)
        if factory_api.caller(request)["station"]:
            _err(ValueError("a person asks for a summary again, not a station"), 403)
        if not accepted:
            _err(ValueError(f"{m['name']} is not accepted yet, so it has no summary"), 409)
        return summaries.rewrite(uid, m["id"])

    @app.post("/api/projects/{uid}/milestones/{ref}/accept")
    def accept_milestone(uid: str, ref: str, request: Request, body: dict = Body(default={})):
        """The person accepts a milestone as it is (TR-1017): every ticket merged, whatever checks
        are still open. Its item leaves the person's waiting list."""
        _project_or_404(uid)
        m = _milestone_or_404(uid, ref)
        if factory_api.caller(request)["station"]:
            _err(ValueError("a person accepts a milestone, not a station"), 403)
        person = _current_person(request)
        store.accept_milestone(uid, m["id"], _by(request, body), (person or {}).get("id"), now_utc())
        summaries.queue(uid, m["id"])   # written in the background (TR-1645); never raises
        # the answer to the milestone's home item (TR-1450), in the words the item asked
        from . import factory_waiting as _fw
        place = next((i for i, x in enumerate(store.milestones_by_project([uid]).get(uid) or [], 1)
                      if x["id"] == m["id"]), 1)
        answers_mod.record(store, request, item=f"milestone_to_check:{uid}:{m['id']}",
                           kind="milestone_to_check", project=uid,
                           ask=_fw.milestone_ask(store.get_project(uid)["name"], place),
                           choice="yes", said="Accepted",
                           person_id=(person or {}).get("id")
                           or ("local" if _is_system(request) else None))
        return next(x for x in _milestones_out(uid) if x["id"] == m["id"])

    @app.post("/api/projects/{uid}/milestones", status_code=201)
    async def create_project_milestone(uid: str, body: dict = Body(...)):
        """{name, goal?, checks?: [{check, expect}], position?}. A milestone with the same name
        is changed instead of added twice, keeping the goal and checks the body leaves out. When
        the project's tickets live in Linear or GitHub, a new milestone is created there through
        the tickets integration (TR-851), and one that exists only in Tares takes its place; its
        name and order change there afterwards (`position` is ignored)."""
        await asyncio.to_thread(_project_or_404, uid)
        try:
            name, goal, checks = factory_mod.milestone_ok(body.get("name"), body.get("goal"),
                                                          body.get("checks"))
        except docs_mod.DocError as e:
            _err(e)
        home = await asyncio.to_thread(store.project_ticket_home, uid)
        cur = await asyncio.to_thread(store.get_milestone, uid, name)
        if home and (cur is None or cur["owner"] == "tares"):
            cls = integrations_registry.get("tickets", home)
            if cls is None:
                _err(ValueError(f"this project's milestones live in {_home_title(home)}: add it "
                                "there, then write its checks here"), 409)
            try:
                refs = await cls(store).create(uid, [{"name": name, "goal": goal}], [])
            except (_linear.LinearError, _gh_tickets.GitHubTicketsError) as e:
                (_linear_err if home == "linear" else _gh_err)(e)
            except (ValueError, KeyError) as e:
                _err(e)
            if len(refs) != 1:
                _err(ValueError(f"{_home_title(home)} did not answer with the milestone"), 502)
            # a sync may have mirrored it meanwhile: the row is found by its external id, else the
            # Tares milestone of the name takes it over
            mid = await asyncio.to_thread(
                store.adopt_milestone, uid, home, refs[0].id, name,
                goal if "goal" in body else None, checks if "checks" in body else None)
            return await asyncio.to_thread(
                lambda: next(m for m in _milestones_out(uid) if m["id"] == mid))

        def _write() -> dict:
            pos = body.get("position")
            if cur:
                fields = {k: v for k, v in (("goal", goal), ("checks", checks)) if k in body}
                if pos is not None and cur["owner"] == "tares":
                    fields["position"] = float(pos)
                store.update_milestone(cur["id"], **fields)
                mid = cur["id"]
            else:
                mid = store.create_milestone(uid, name, goal, checks,
                                             float(pos) if pos is not None else None)
            return next(m for m in _milestones_out(uid) if m["id"] == mid)
        return await asyncio.to_thread(_write)

    @app.put("/api/projects/{uid}/milestones/{ref}")
    def update_project_milestone(uid: str, ref: str, body: dict = Body(...)):
        """Change a milestone's name, goal, checks or position. A Linear or GitHub milestone only
        takes goal and checks (its name and order change where it lives)."""
        _project_or_404(uid)
        m = _milestone_or_404(uid, ref)
        if m["owner"] != "tares" and ({"name", "position"} & set(body)):
            _err(ValueError(f"{m['name']} lives in {_home_title(m['owner'])}: rename or reorder "
                            "it there"), 409)
        fields: dict = {}
        try:
            if "name" in body:
                fields["name"] = docs_mod.title_ok(body["name"])
                other = store.get_milestone(uid, fields["name"])
                if other and other["id"] != m["id"]:
                    _err(ValueError(f"the project already has a milestone {other['name']!r}; "
                                    "milestone names are unique"), 409)
            if "goal" in body:
                fields["goal"] = factory_mod.goal_ok(body["goal"])
            if "checks" in body:
                fields["checks"] = factory_mod.checks_ok(body["checks"])
        except docs_mod.DocError as e:
            _err(e)
        if body.get("position") is not None:
            fields["position"] = float(body["position"])
        if fields:
            store.update_milestone(m["id"], **fields)
        return next(x for x in _milestones_out(uid) if x["id"] == m["id"])

    @app.delete("/api/projects/{uid}/milestones/{ref}")
    def delete_project_milestone(uid: str, ref: str):
        """Delete a Tares milestone; its tickets stay, without a milestone."""
        _project_or_404(uid)
        m = _milestone_or_404(uid, ref)
        if m["owner"] != "tares":
            _err(ValueError(f"{m['name']} lives in {_home_title(m['owner'])}: remove it there"),
                 409)
        store.delete_milestone(m["id"])
        return {"ok": True, "deleted": m["id"]}

    # ── the Claude Code sessions that worked in a project (TR-405) ────────────
    @app.get("/api/projects/{uid}/sessions")
    def list_project_sessions(uid: str):
        """The sessions that worked in the project (the spec session among them), newest activity
        first, each with its state (working, idle between turns, waiting for the person, ended) and, for a waiting
        one, the last thing it said (`last_said`). A session that also worked in a private project
        the caller cannot see is not listed, unless it is the caller's own station's (TR-1710)."""
        _project_or_404(uid)
        src = _cc_source()   # the one source session lines are stored under (TR-1804)
        out = store.project_sessions(uid)
        unread = factory_api.unread_sessions(h, [s["session"] for s in out])
        out = [s for s in out if s["session"] not in unread]
        now = now_utc()
        for s in out:
            # a session whose end never reached Tares does not wait forever (TR-473)
            s["state"] = factory_mod.shown_state(s["state"], s["last_at"], now)
            if s["state"] == "waiting":
                said = store.last_said(src, s["session"]) or ""
                s["last_said"] = said[:1200]
        return {"sessions": out, "now": now}

    @app.get("/api/projects/{uid}/sessions/{sid}")
    def get_project_session(uid: str, sid: str, limit: int = 200, offset: int = 0):
        """One session's lines, oldest first, the ones from before it named the project
        included. A session the list leaves out for this caller answers as one that did not work
        here (TR-1710)."""
        _project_or_404(uid)
        if (not any(s["session"] == sid for s in store.project_sessions(uid))
                or factory_api.unread_sessions(h, [sid])):
            _err(KeyError(f"session {sid!r} did not work in this project"), 404)
        src = _cc_source()   # the one source session lines are stored under (TR-1804)
        lines, where = store.key_events_from(src, sid, limit=min(max(limit, 1), 1000),
                                             offset=max(offset, 0))
        return {"session": sid, "lines": lines, "lines_from": where}

    # ── picking up a build where a session stopped: what a fresh session needs to carry on the
    # ticket in progress, from what Tares recorded (works after a crash too) ────────────────
    _QUIET_MIN = 10   # a working session that sent nothing for this long is taken as gone

    def _cc_source() -> str:
        """The source session lines are stored under: always `claude_code` (TR-1804)."""
        from .session_map import SOURCE as SESSION_SOURCE
        return SESSION_SOURCE

    def _previous_session(sessions: list[dict], ticket: dict | None) -> dict | None:
        """The session that was working on the ticket: active when it went in progress (its
        lines span that moment), else the latest one that started before it, else the latest."""
        live = [s for s in sessions if s["state"] != "replaced"]
        if not live:
            return None
        t = ticket["updated_at"] if ticket else None
        if t is not None:
            spans = [s for s in live if s["started_at"] and s["last_at"]
                     and s["started_at"] <= t <= s["last_at"]]
            if spans:
                return max(spans, key=lambda s: s["last_at"])
            before = [s for s in live if s["started_at"] and s["started_at"] <= t]
            if before:
                return max(before, key=lambda s: s["last_at"] or s["started_at"])
        return live[0]

    def _pickup(uid: str) -> tuple[dict, dict | None]:
        """The hand-over as the caller may see it, and the session that was on the ticket, which
        the caller may not read (TR-1710): then `previous_session` in the hand-over is None."""
        tickets = store.list_tickets(uid)
        ready = factory_mod.readiness(tickets)
        # the ticket a session stopped on: in progress, and not waiting on someone else (in
        # review, changes asked, blocked: picking those up would redo work that is done)
        ticket = next((t for t in tickets if t["status"] == "in_progress"
                       and t["stage"] not in ("review", "changes", "blocked", "merged",
                                              "shipped")), None)
        nxt = ticket or next((t for t in tickets if t["status"] == "todo"
                              and ready[t["id"]]["ready"]), None)
        sessions = store.project_sessions(uid)
        prev = _previous_session(sessions, ticket) if ticket else None
        out: dict = {"ticket": _ticket_out(uid, nxt, with_doc=True) if nxt else None,
                     "in_progress": ticket is not None, "previous_session": None,
                     "may_be_open": False}
        if prev:
            now = now_utc()
            quiet = int((now - prev["last_at"]).total_seconds() // 60) if prev["last_at"] else None
            open_ = prev["state"] in ("idle", "waiting") or (
                prev["state"] in (None, "working") and quiet is not None and quiet < _QUIET_MIN)
            src = _cc_source()
            # no other session takes its place: the build was on this one, readable or not
            if not factory_api.unread_sessions(h, [prev["session"]]):
                out["previous_session"] = {
                    "session": prev["session"], "repo": prev["repo"], "state": prev["state"],
                    "reason": prev["state_reason"], "last_at": prev["last_at"],
                    "quiet_minutes": quiet,
                    "said_lately": store.said_lately(src, prev["session"], 3),
                    "files_touched": store.files_touched(src, prev["session"])}
            out["may_be_open"] = bool(open_)
        gmem = _global_ids().get("memory")
        mem = next((d for d in store.list_docs(uid, "memory") if d["id"] != gmem), None)
        body = (store.get_doc(uid, mem["id"]) or {}).get("body", "") if mem else ""
        if len(body) > 6000:   # newest last: keep the newest lines
            body = "...\n" + body[-6000:].split("\n", 1)[-1]
        out["memory"] = body
        out["decisions"] = [{k: d[k] for k in ("label", "question", "words", "choice", "at")}
                            for d in store.list_decisions(uid, all_=True)[:10]]
        notes = [d for d in store.list_docs(uid, "note")]
        out["notes"] = [{"id": d["id"], "title": d["title"],
                         "body": (store.get_doc(uid, d["id"]) or {}).get("body", "")[:4000]}
                        for d in notes]
        out["checklist"] = (
            [f"Check the branch and uncommitted changes (git status, git log -3) before editing: "
             f"the previous session may have left work on ticket \"{ticket['title']}\" "
             "uncommitted.",
             "Restart what the project runs on (local database, dev server) the way AGENTS.md "
             "says; the previous session's processes are gone.",
             "Read the ticket's working doc, its Progress section first, then the previous "
             "session's last messages: they say what was done and what was left.",
             "Finish what is left, run the ticket's Verify, commit, mark it done, then carry on "
             "with the next ticket."]
            if ticket else
            ["No ticket is in progress: start with the next one, the way the starting prompt says."]
            if nxt else
            ["No ticket is ready: " + "; ".join(
                f"{factory_mod.label(t)} waits for {', '.join(ready[t['id']]['blocked_by'])}"
                for t in tickets if t["status"] == "todo" and ready[t["id"]]["blocked_by"])
             + ". Finish or re-plan what they wait for." if any(
                t["status"] == "todo" for t in tickets) else
             "Every ticket is finished." if tickets else
             "The project has no tickets yet: spec it first (/tares:spec)."])
        return out, prev

    @app.get("/api/projects/{uid}/pickup")
    def get_pickup(uid: str):
        """What a fresh session needs to pick the build up where a session stopped: the ticket in
        progress (else the next) with its working doc, the session that was on it (its state,
        last messages, files it touched, whether it may still be open), the builders' notes and
        a checklist. `previous_session` is null when that session also worked in a private project
        the caller cannot see and is not their own station's (TR-1710). Reads only."""
        _project_or_404(uid)
        return _pickup(uid)[0]

    @app.post("/api/projects/{uid}/pickup")
    def take_over(uid: str, body: dict = Body(default={})):
        """A fresh session takes the build over: the previous session is marked replaced, so two
        sessions never work the same ticket. When it may still be open (waiting, or active in
        the last minutes) this needs {"take_over": true}. A caller who must not read that session
        takes the build over all the same, and is told neither its state nor its id (TR-1710)."""
        _project_or_404(uid)
        out, prev = _pickup(uid)
        shown = out["previous_session"]
        if prev and out["may_be_open"] and not body.get("take_over"):
            state = f" ({shown['state'] or 'active'})" if shown else ""
            _err(ValueError(f"the session that was building may still be open{state}; ask the "
                            "person, then take over with take_over"), 409)
        if prev:
            store.set_session_state(prev["session"], "replaced",
                                    "a fresh session picked the build up", now_utc(), force=True)
        return {**out, "taken_over": shown["session"] if shown else None}

    # ── the crew's standup report: before factory_api, whose /api/crew/{name} would take it ──
    def _standup_range(since: str | None, until: str | None) -> tuple[datetime, datetime]:
        def parse(v: str, what: str) -> datetime:
            try:
                d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            except ValueError:
                _err(ValueError(f"{what} is not a date and time: {v!r}"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        end = parse(until, "until") if until else now_utc()
        start = parse(since, "since") if since else end - timedelta(days=1)
        if start >= end:
            _err(ValueError("since must be before until"))
        if end - start > timedelta(days=92):
            _err(ValueError("a standup covers at most 92 days"))
        return start, end

    def _standup(since: str | None, until: str | None) -> dict:
        start, end = _standup_range(since, until)
        return standup_mod.facts(
            store, start, end,
            lambda uid, tickets, ms: factory_api.project_progress(store, uid, tickets, ms),
            hide=_hidden_ids())

    @app.get("/api/crew/standup")
    def crew_standup(since: str | None = None, until: str | None = None):
        """What moved in each project handed to the crew between `since` and `until` (ISO; the
        last day by default), read from the ledger: done, started, in review, blocked, and the
        questions waiting on the person. `markdown` is the same report ready to copy."""
        report = _standup(since, until)
        return {**report, "markdown": standup_mod.markdown(report)}

    async def _summarise_standup(report: dict, timeout: int | None = None) -> tuple[dict | None, str | None]:
        """A line or two per project written by the cell's model, as (summary, why not). Never
        raises: the standup stands on its facts alone."""
        wait = timeout or 90
        summary, why = None, None
        provider, origin = resolve_provider(store)
        if provider is None:
            why = "no model provider is set up"
        elif report["projects"]:
            pid = providers_mod.default_id(store)
            model = providers_mod.default_model_for(store, pid)
            try:
                reply = await asyncio.wait_for(provider.complete(
                    model=model, system=standup_mod.SUMMARY_SYSTEM, tools=[],
                    messages=[{"role": "user", "content": standup_mod.summary_prompt(report)}],
                    max_tokens=standup_mod.SUMMARY_MAX_TOKENS, tools_allowed=False), timeout=wait)
                # one reply's usage has no call count; the ledger row wants the loops' total
                usage = empty_usage()
                add_usage(usage, reply.usage)
                _record_model_usage(model, usage, key_source=origin, kind=provider.kind,
                                  provider_id=pid)
                summary = standup_mod.parse_summary(reply.text,
                                                    {p["id"] for p in report["projects"]})
                if not summary["overall"] and not summary["projects"]:
                    summary, why = None, "the model's answer could not be read"
            except asyncio.TimeoutError:
                why = f"the model took longer than {wait}s"
            except Exception as e:  # noqa: BLE001 — the report stands without the summary
                msg = str(e)
                why = ("the model provider refused its key; check it under Settings"
                       if any(c in msg for c in (" 401", " 403", "invalid x-api-key"))
                       else f"the model call failed ({type(e).__name__})")
                print(f"standup: summary failed: {type(e).__name__}: {msg[:300]}", flush=True)
        return summary, why

    @app.post("/api/crew/standup/summary")
    async def crew_standup_summary(body: dict = Body(default={})):
        """{since?, until?}: the same report with a line or two per project written by the cell's
        model from those facts. Without a model provider, or when it fails, the report comes back
        without the summary and `summary_error` says why."""
        report = _standup(body.get("since"), body.get("until"))
        summary, why = await _summarise_standup(report)
        return {**report, "summary": summary, "summary_error": why,
                "markdown": standup_mod.markdown(report, summary)}

    async def _post_slack(channel: str, text: str, title: str = "") -> dict:
        """Post markdown to a Slack channel as the cell's bot. LookupError when Slack is not
        connected, RuntimeError with Slack's reason when it does not take the message."""
        token, _origin = resolve_slack_token(store)
        if not token:
            raise LookupError("Slack is not connected: add it under Settings, Slack")
        msg = {"channel": channel, "text": slack_mod.excerpt(text, 300) or title or "Tares",
               "blocks": ([slack_mod._header(title)] if title else []) + slack_mod._sections(text),
               "unfurl_links": False, "unfurl_media": False}
        try:
            async with httpx.AsyncClient(timeout=15) as cx:
                r = await cx.post(f"{slack_mod.API_BASE}/chat.postMessage", json=msg,
                                  headers={"authorization": f"Bearer {token}"})
            try:
                data = r.json()
            except ValueError:
                data = None
        except httpx.HTTPError as e:
            raise RuntimeError(f"could not reach Slack: {type(e).__name__}") from None
        ok, error, _retry = slack_mod.classify(r.status_code, data)
        if not ok:
            raise RuntimeError(error or "Slack refused the message")
        return data or {}

    @app.post("/api/slack/post")
    async def slack_post(request: Request, body: dict = Body(...)):
        """{channel, text, title?}: post markdown to a Slack channel as the cell's bot (the
        standup's "Send to Slack"). A person's action: refused from a crew station."""
        if factory_api.caller(request)["station"]:
            _err(PermissionError("posting to Slack is the person's action, from the console"), 403)
        channel = str(body.get("channel") or "").strip()
        text = str(body.get("text") or "").strip()
        if not channel or not text:
            _err(ValueError("say which channel and what to post"))
        title = " ".join(str(body.get("title") or "").split())
        try:
            data = await _post_slack(channel, text, title)
        except LookupError as e:
            _err(e, 409)
        except RuntimeError as e:
            _err(e, 502)
        return {"ok": True, "channel": channel, "ts": data.get("ts")}

    # The morning status (TR-825) reuses the standup above. Its report leaves private projects out:
    # it runs with no caller and goes to a shared place.
    _morning = morning_mod.MorningStatus(
        store,
        report=lambda since, until: standup_mod.facts(
            store, since, until,
            lambda uid, tickets, ms: factory_api.project_progress(store, uid, tickets, ms),
            hide={p["id"] for p in store.private_projects()}),
        summarise=_summarise_standup,
        post_slack=_post_slack,
        slack_connected=lambda: bool(resolve_slack_token(store)[0]))
    app.state.morning = _morning

    # ── the software factory's crew (M3 onward): routes in tares/factory_api.py ──
    def _factory_project_id(ref: str) -> str:
        """A project's id from its id or name (the ref itself when there is none: a 404 later)."""
        p = store.get_project(ref) or store.get_project_by_name(ref)
        return p["id"] if p else ref

    h = Host(
        store,
        err=lambda e, code=400: _err(e, code), project_or_404=lambda uid: _project_or_404(uid),
        by=lambda request, body=None: _by(request, body), cc_source=lambda: _cc_source(),
        current_person=lambda request: _current_person(request),
        area_owner=lambda repo: team_mod.get().area_owner(store, repo),
        is_system=lambda request: _is_system(request),
        is_admin=lambda request: _is_admin(request),
        hidden_ids=lambda: _hidden_ids(), can_see=lambda p: _can_see(p),
        read_exclude=lambda: _read_exclude(),
        global_ids=lambda create=False: _global_ids(create), resolve_project=_factory_project_id,
        ticket_out=lambda uid, t, **kw: _ticket_out(uid, t, **kw),
        ensure_globals=lambda uid: _ensure_globals(uid),
        tickets_out=lambda uid: _tickets_out(uid),
        caller=_CALLER.get, project_key=_pk, hidden_project=_hidden_project,
        auth_off=lambda: not AUTH_TOKEN,
        projects=lambda: projects,
        licence_state=lambda: licence.current(store),
        licence_required=licence.licence_required)
    app.state.host = h
    fx = factory_api.register(app, store, h)
    # a direct message on Slack when something new waits for a person (TR-1458)
    _waiting_slack = waiting_slack_mod.WaitingSlack(store, fx.waiting_for_person)
    waiting_slack_mod.register(app, store, h, _waiting_slack)
    app.state.waiting_slack = _waiting_slack
    # the edition screens (tares/screens/__init__.py lists them)
    screens.register_all(app, store, h, SimpleNamespace(fx=fx, morning=_morning))
    integrations_api.register(app, store, h)
    guardrails_api.register(app, store, h)
    project_secrets.register(app, store, h)   # TR-775

    @app.get("/api/home")
    async def home(request: Request):
        """One read for the home screen of the cell's edition; the platform has none."""
        value, _needed = edition_mod.edition(store)
        if value == "factory":
            # one page read: its repeated small reads run once (TR-1631)
            return await asyncio.to_thread(request_memo.run, factory_home.build, store, fx, request)
        return JSONResponse({"error": "no home on this edition"}, status_code=404)

    # what the Claude Code sessions spend: the plan's limits and tokens by project
    claude_usage.register(app, store, _hidden_ids)

    # ── Linear (TR-408): the cell's connection, and the Linear project a project's tickets live
    # in. Tares reads from Linear; write_ticket and write_milestone create there through Tares ──
    def _linear_err(e: Exception):
        _err(e, 502 if "reach" in str(e) or "answered" in str(e) else 400)

    @app.get("/api/linear")
    def linear_status():
        """Whether Linear is connected (never the token), and as whom."""
        return _linear.public_connection(store)

    @app.post("/api/linear")
    async def linear_connect_key(body: dict = Body(...)):
        """{api_key}: connect with a personal API key, checked against Linear first."""
        try:
            await _linear.connect_api_key(store, str(body.get("api_key") or ""))
        except _linear.LinearError as e:
            _linear_err(e)
        return _linear.public_connection(store)

    @app.put("/api/settings/linear-connection")
    def linear_cloud_set(body: dict = Body(...)):
        """Tares Cloud only, from the control plane (TR-537): {token, expires_at, account} after a
        sign-in with the GlassFlow Linear app or a renewal, or {error, account} when Linear refused
        to renew it. No restart; the next sync uses it."""
        try:
            _linear.set_cloud_connection(store, body)
        except _linear.LinearError as e:
            _err(e)
        return _linear.public_connection(store)

    @app.delete("/api/settings/linear-connection")
    def linear_cloud_clear():
        """Tares Cloud only: the control plane disconnected this workspace (and revoked the token
        at Linear)."""
        _linear.disconnect(store)
        return _linear.public_connection(store)

    @app.delete("/api/linear")
    def linear_disconnect():
        """Forget the connection. Linked projects keep their tickets and stop syncing until
        Linear is connected again. On Tares Cloud the console disconnects through the control
        plane instead, which also revokes the token at Linear."""
        _linear.disconnect(store)
        return _linear.public_connection(store)

    @app.get("/api/linear/projects")
    async def linear_projects(q: str = ""):
        """Linear projects this connection can see, most recently updated first, for a picker."""
        try:
            return {"projects": await _linear.list_projects(store, q)}
        except _linear.LinearError as e:
            _linear_err(e)

    async def _linear_sync_now(uid: str) -> None:
        # the same lock the sync runner holds, so the two never write one project's tickets at once
        from .integrations.sync import project_lock
        async with project_lock("linear", uid):
            await _linear.sync_project(store, uid)   # its change events are not stored (TR-1806)

    @app.post("/api/projects/{uid}/linear/sync")
    async def sync_linear_project(uid: str):
        """Sync the project's tickets with Linear now."""
        _project_or_404(uid)
        if not store.get_project_linear(uid):
            _err(ValueError("this project does not use Linear"), 409)
        try:
            await _linear_sync_now(uid)
        except _linear.LinearError as e:
            _linear_err(e)
        return {"tickets": _tickets_out(uid),
                "linear": store.get_project_linear(uid)}

    @app.delete("/api/projects/{uid}/linear")
    def unlink_linear(uid: str):
        """The project stops using Linear. Its tickets stay, now owned by Tares, with their
        working docs."""
        _project_or_404(uid)
        store.set_project_linear(uid, None)
        for t in store.list_tickets(uid):
            if t["owner"] == "linear":
                store.update_ticket_owner(t["id"], "tares")
        for m in store.list_milestones(uid):
            if m["owner"] == "linear":
                store.update_milestone(m["id"], owner="tares", external_id=None)
        return {"tickets": _tickets_out(uid),
                "linear": None}

    # ── GitHub Issues (TR-569): the repo + issue label a project's tickets live in. Tares only
    # reads from GitHub; sessions write with the `gh` CLI ────────────────────────────────────
    def _gh_err(e: Exception):
        if getattr(e, "rate_limited", False):
            _err(e, 429)
        _err(e, 502 if "reach" in str(e) or "answered" in str(e) else 400)

    async def _github_sync_now(uid: str) -> None:
        # the same lock the sync runner holds, so the two never write one project's tickets at once
        from .integrations.sync import project_lock
        async with project_lock("github", uid):
            await _gh_tickets.sync_project(store, uid)   # its change events are not stored (TR-1806)

    @app.post("/api/projects/{uid}/github/sync")
    async def sync_github_issues(uid: str):
        """Sync the project's tickets with GitHub now."""
        _project_or_404(uid)
        link = store.get_project_github(uid)
        if not link:
            _err(ValueError("this project does not use GitHub Issues"), 409)
        if _gh_tickets._backing_off(link):
            # a sync now would be skipped, so say so instead of answering 200 with old tickets
            wait = (datetime.fromisoformat(link["backoff_until"]) - now_utc()).total_seconds()
            raise HTTPException(status_code=429, detail="GitHub rate limit reached",
                                headers={"Retry-After": str(max(1, int(wait) + 1))})
        try:
            await _github_sync_now(uid)
        except _gh_tickets.GitHubTicketsError as e:
            _gh_err(e)
        return {"tickets": _tickets_out(uid), "github": store.get_project_github(uid)}

    @app.delete("/api/projects/{uid}/github")
    def unlink_github(uid: str):
        """The project stops using GitHub Issues. Its tickets stay, now owned by Tares, with their
        working docs."""
        _project_or_404(uid)
        store.set_project_github(uid, None)
        for t in store.list_tickets(uid):
            if t["owner"] == "github":
                store.update_ticket_owner(t["id"], "tares")
        for m in store.list_milestones(uid):
            if m["owner"] == "github":
                store.update_milestone(m["id"], owner="tares", external_id=None)
        return {"tickets": _tickets_out(uid), "github": None}

    # ── a project's keys (TR-335) and stats over its sources ─────────────────────────────────
    def _project_or_404(uid: str) -> dict:
        p = store.get_project(uid)
        if p is None or not _can_see(p):   # a private one the caller cannot see is unknown
            _err(KeyError(f"unknown project {uid!r}"), 404)
        return p

    @app.get("/api/projects/{uid}/keys")
    def list_project_keys(uid: str):
        """The project's active keys, without their secrets."""
        _project_or_404(uid)
        return {"keys": [k for k in store.list_api_keys(project=uid) if not k["revoked_at"]],
                "enforced": bool(AUTH_TOKEN)}

    @app.post("/api/projects/{uid}/keys", status_code=201)
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

    # ── metering ──────────────────────────────────────────────────────────────
    @app.get("/api/usage")
    def usage():
        """What this instance is using: db + WAL bytes, the volume they sit on, and event counts.
        `max_bytes` is the operator's TARES_MAX_DB_SIZE, else the volume the database sits on
        (`max_bytes_source` says which); null only when neither is known. At INGEST_PAUSE_PCT of it
        ingest is refused (`ingest_paused`), so the disk never fills.
        `pct_used` is (db + wal) over max_bytes on a 0-100 scale, NOT a 0-1 fraction — so a
        "warn at 80%" consumer compares against 80, not 0.8. Null whenever max_bytes is null.
        Cheap by construction: file stats plus the maintained per-source counters, no table scan,
        so the cost does not grow with the event count."""
        u = store.usage()
        try:
            du = shutil.disk_usage(os.path.dirname(os.path.abspath(DB_PATH)) or ".")
            disk_total, disk_free = du.total, du.free
        except OSError:
            disk_total = disk_free = None
        st = storage_state(store, DB_PATH)
        return {**u, "disk_total": disk_total, "disk_free": disk_free,
                "max_bytes": st["max_bytes"], "max_bytes_source": st["max_bytes_source"],
                "pct_used": st["pct_used"], "ingest_paused": st["paused"]}

    @app.get("/api/usage/model")
    def usage_model(days: int = 30):
        """The cell's Anthropic spend meter: all-time totals plus a per-day tail, split by surface
        (every summary and setup plan is under "ask", the name it had before agents went; old
        agent runs stay under "agent"). Generic data only — a hosted control plane polls this to enforce
        credits, a self-hoster reads their own bill coming; the cell attaches no meaning to it.
        `cost_usd` is a floor: `uncosted_calls` counts rows whose model had no known price, and
        runs from before metering existed are absent entirely."""
        return store.model_usage_summary(days=max(1, min(int(days), 366)))

    # ── the team seam (TR-914): the order every route registration follows ──
    # FastAPI answers with the first route that matches, so the order is the contract:
    #   1. core routes, factory_api.register and screens.register_all (all above);
    #   2. the ee team's routes, when tares_ee loads, whatever the licence (each ee route answers
    #      licence_required itself while the licence does not allow it);
    #   3. the fallbacks: one route per path that moved to ee, answering licence_required when no
    #      ee team registered it (an ee route registered in 2 wins over its fallback);
    #   4. the console catch-all below, last.
    # A route that moves to ee is deleted from core in the same PR, so no core route shadows it.
    team_mod.register_ee(app, h)   # never raises: a broken ee team leaves a single-person cell
    team_mod.register_fallbacks(app, h)

    # ── console UI (built SPA; catch-all registered last so API routes win) ──
    @app.get("/{path:path}", include_in_schema=False)
    def ui(path: str):
        # An unmatched /api/* or /ingest/* path must never answer with the SPA's HTML and a 200 —
        # a client checking the status reads that as success and fails to parse (TR-226). The
        # console never requests these prefixes, so nothing legitimate is lost.
        if path == "api" or path.startswith(("api/", "ingest/")):
            _err(KeyError(f"unknown API path /{path}"), 404)
        return _serve_ui(path)

    return app
