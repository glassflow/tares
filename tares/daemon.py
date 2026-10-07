"""taresd — the always-on daemon. Owns the store; runs connector loops + trigger eval; serves
the local HTTP API (agent surface + management API) and the built-in console UI.

The catalog lives in the store (DB-backed). On first boot with an empty catalog, the YAML file at
TARES_CATALOG is imported once; from then on YAML is an import/export format, and all source/
trigger/agent/project management happens over /api (or the UI at /).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import ipaddress
import os
import socket
import time
import re
import secrets
import shutil
import traceback
import uuid
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response, StreamingResponse)
from pydantic import BaseModel, model_validator

from .config import (SLACK_URL_PREFIX, CatalogError, agent_url, catalog_from_db, export_db_to_yaml,
                     validate_mcp_server_dict,
                     import_yaml_to_db, slack_channel_from_url, slack_url,
                     validate_agent_dict, validate_slack_channel, validate_source_dict,
                     check_handoff_targets, normalize_handoffs, normalize_verdicts,
                     validate_trigger_dict, normalize_trigger_description, VIEWS_REMOVED,
                     _source_from_dict)
from .connectors import (SPECS, normalize_config, redact_config, restore_secrets,
                         source_type_for)
from .dispatch import Dispatcher
from .envelope import now_utc
from .builtin_agents import (AGENT_MODELS, MODEL as AGENT_DEFAULT_MODEL, offers_conclude,
                             MAX_ROUNDS as AGENT_MAX_ROUNDS,
                             MAX_ROUNDS_LIMIT as AGENT_MAX_ROUNDS_LIMIT,
                             MAX_ROUNDS_WITH_MCP as AGENT_MAX_ROUNDS_WITH_MCP,
                             PRESETS as AGENT_PRESETS, AgentRunner, effective_max_rounds,
                             resolve_anthropic_headers, resolve_api_base, resolve_provider,
                             DEFAULT_API_BASE)
from . import metrics as _metrics
from . import docs as docs_mod
from . import linear as _linear
from . import skills as skills_mod
from . import providers as providers_mod
from .runtime import Runtime
from . import slack as slack_mod
from . import slack_verify
from . import timeline
from . import goal as goal_mod
from .slack import SETTING_KEY as SLACK_TOKEN_SETTING, resolve_token as resolve_slack_token
from .tracing import PROVIDERS as tracing_providers, status as tracing_status
from .store import Store, StoreUnavailable
from .projects import Engine as ProjectEngine, ProjectError
from .reads import resolve_read

SLACK_TEAM_SETTING = "slack_team"   # JSON {id, name}: the Slack team the bot token belongs to

CATALOG_PATH = os.getenv("TARES_CATALOG", "catalog.yaml")
# Renamed in 1.0 along with everything else. An install that upgrades without moving its file is
# caught by config.reject_legacy_db(), which refuses to start rather than silently creating an empty
# database next to the old one — DuckDB would happily do exactly that.
DB_PATH = os.getenv("TARES_DB", "tares.duckdb")
# Re-import the catalog YAML on every boot (declarative: the file is the source of truth, so an
# operator manages a read-only demo's sources by editing the YAML and restarting).
CATALOG_SYNC = os.getenv("TARES_CATALOG_SYNC", "").strip().lower() in ("1", "true", "yes", "on")

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
# The Anthropic key for the in-app Ask agent (and Tares agents) is resolved at request time via
# resolve_anthropic_headers(store): Resolve headers from the console-stored key,
# then ANTHROPIC_AUTH_TOKEN, then ANTHROPIC_API_KEY.
# Never returned by any API — capabilities exposes only a boolean.
# The Slack bot token behind the slack:// dispatch sink keeps the OPPOSITE order via
# resolve_slack_token(store): env TARES_SLACK_BOT_TOKEN wins over the console-stored value
# (infrastructure wiring, not a metered credential — see the note on resolve_token).

# Seed a project on first boot (additive, env-gated): the named template is created once, so a
# hosted cell is born with its demo already running instead of an empty system. A settings
# marker makes it one-shot — deleting the project never resurrects it. Meaningful for a
# self-hoster building a golden image too; harmlessly unset otherwise.
# TARES_SEED_USECASE is the old name, accepted until two releases after 1.14.
SEED_PROJECT = (os.getenv("TARES_SEED_PROJECT", "") or os.getenv("TARES_SEED_USECASE", "")).strip()


_STARTED_MONO = time.monotonic()


def _installed_version() -> str | None:
    from importlib.metadata import version as _v
    try:
        return _v("tares")
    except Exception:
        return None


def _is_ingest(path: str) -> bool:
    return path.startswith("/ingest/") or path in ("/v1/logs", "/v1/traces", "/v1/metrics")


def _bearer(auth: str | None) -> str:
    return auth[7:].strip() if auth and auth.lower().startswith("bearer ") else ""


BYPASS_COOLDOWN_HEADER = "X-Tares-Bypass-Cooldown"


def _bypass_cooldown(request: Request) -> bool:
    """Whether this delivery is an on-demand one that must wake its triggers even inside their
    cooldown. Anything but `1` or `true` (in any case) is an ordinary delivery."""
    return request.headers.get(BYPASS_COOLDOWN_HEADER, "").strip().lower() in ("1", "true")


SLACK_EVENTS_PATH = "/api/slack/events"
SLACK_CHANNELS_TTL = 60.0         # seconds a good channel list is served from the cell
SLACK_CHANNELS_FAIL_TTL = 5.0     # ... and a failed one at least (longer on a 429's Retry-After)

# Where GitHub sends the browser back during "Create GitHub App" and after an install. A redirect
# carries no Tares token, so these two GETs are public to the auth middleware. The creation callback
# needs our signed `state`; the install callback needs the state, or an installation on the App
# owner's account (see github_app_installed).
# GitHub's hosted MCP server: agents read and write repos through it with a stored credential
# (TR-348: it accepts a GitHub App's installation tokens, writes then show as the App)
GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"

LINEAR_CALLBACK = "/api/linear/oauth/callback"   # Linear sends the browser back here (TR-408)
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
    if method == "GET" and path == LINEAR_CALLBACK:
        return True      # gated by the signed state and the PKCE verifier only this process holds
    return method in ("GET", "HEAD") and not (
        path.startswith("/api/") or path.startswith("/catalog") or path == "/query")


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
# Above this share of the limit ingest is refused (507) and polls pause, so the database never
# fills its disk and dies (glassflow-web, 2026-09-08). Reads, findings and the console keep working.
INGEST_PAUSE_PCT = float(os.getenv("TARES_INGEST_PAUSE_PCT", "95"))


def _ui_dist() -> Path:
    """Where the built console SPA lives. Prefer the copy shipped inside the wheel
    (tares/console, via hatch force-include); fall back to the source tree's ui/dist for
    `npm run dev`-style local work. TARES_UI_DIST overrides both."""
    if env := os.getenv("TARES_UI_DIST"):
        return Path(env)
    from importlib import resources
    packaged = resources.files("tares") / "console"   # present in an installed wheel
    if packaged.is_dir():
        return Path(str(packaged))
    return Path(__file__).resolve().parent.parent / "ui" / "dist"   # running from the source tree


UI_DIST = _ui_dist()

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

# How long a `/tares ask` may think before Slack gets an apology instead of an answer. The user
# is staring at a "…" in a channel, so this is deliberately much shorter than the agent's own
# round budget would allow.
SLACK_ASK_TIMEOUT = float(os.getenv("TARES_SLACK_ASK_TIMEOUT", "120"))


def _serve_ui(path: str):
    """The built console SPA: a real file if it exists, else index.html (client-side routing)."""
    if not UI_DIST.exists():
        return JSONResponse(
            {"detail": "console not built; run `npm install && npm run build` in ui/"},
            status_code=404)
    f = (UI_DIST / path).resolve()
    if path and f.is_file() and f.is_relative_to(UI_DIST.resolve()):
        return FileResponse(f)
    return FileResponse(UI_DIST / "index.html")


def _degraded_app(reason: str) -> FastAPI:
    """The app we serve when the store could not be opened. It exists so the failure reaches a
    human instead of ERR_CONNECTION_REFUSED: the console still loads and every data route answers
    503 with the reason, so the UI can name the problem. No runtime, no connectors, no ingest —
    nothing that would need the store. Restart once the DB is reachable again."""
    app = FastAPI(title="taresd (degraded)")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    @app.get("/health", include_in_schema=False)
    async def health():
        # HTTP 200 with status "down": a probe that can read the body learns WHAT is wrong, and the
        # console (which only ever reaches this daemon) can render it. Keys AuthGate depends on stay.
        body = {"status": "down", "detail": reason, "auth_required": bool(AUTH_TOKEN),
                "sources": [], "pct_used": None}
        if LOGIN_URL:
            body["login_url"] = LOGIN_URL
        return body

    async def unavailable():
        return JSONResponse({"detail": f"database unavailable; {reason}", "status": "down"},
                            status_code=503)

    # Everything that needs the store answers 503 (not a bare 500, and not the SPA's index.html).
    for _p in ("/api/{rest:path}", "/query", "/read", "/remember", "/catalog", "/catalog/{rest:path}",
               "/ingest/{rest:path}", "/v1/logs", "/v1/traces", "/v1/metrics"):
        app.add_api_route(_p, unavailable, include_in_schema=False,
                          methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])

    @app.get("/{path:path}", include_in_schema=False)
    async def ui(path: str):
        # An unmatched /api/* or /ingest/* path must never answer with the SPA's HTML and a 200 —
        # a client checking the status reads that as success and fails to parse (TR-226). The
        # console never requests these prefixes, so nothing legitimate is lost.
        if path == "api" or path.startswith(("api/", "ingest/")):
            _err(KeyError(f"unknown API path /{path}"), 404)
        return _serve_ui(path)

    return app


class ReadReq(BaseModel):
    selector: dict = {}          # {label: value, ...} — strict-AND conjunction; must be non-empty
    window: str = "15m"
    client: str = "http"   # http | mcp | ui — tags the read in the activity log
    include_payload: bool = False  # also return the raw lossless record as `raw` on each row
    project: str = ""            # read only this project's sources
    sources: list[str] = []      # read only these sources (within the project when both given)


class SubReq(BaseModel):
    trigger: str
    url: str


class UnsubReq(BaseModel):
    subscription_id: str


class SourceIn(BaseModel):
    name: str
    type: str = ""          # ignored — the signal type is derived from the connector
    connector: str
    poll: str = "5s"
    config: dict = {}
    project: str = ""       # the project the new source joins; "" = the default project


class RememberReq(BaseModel):
    key: str                     # the entity this memory is about
    content: str
    memory_type: str = "observation"   # observation | aggregation | decision | custom
    fields: dict = {}            # extra values, kept in the payload (lossless); not auto-aggregated
    source: str | None = None    # target memory source; default auto-provisions agent_memory


class TriggerIn(BaseModel):
    name: str
    project: str = ""            # "" = the default project on create, unchanged on update
    sources: list[str] = []      # the sources the trigger watches (at least one)
    filters: list[dict] = []     # [{field, op, value}] narrowing them
    key_field: str = ""          # the entity label; "" = the first source's primary label
    condition: dict
    emit: dict = {}
    cooldown: str = "5m"
    description: str | None = None  # what wakes it in plain words; None keeps it on update
    view: str | None = None      # only to refuse it by name: views were removed


class AgentIn(BaseModel):
    name: str = ""               # required on create; PUT fills it from the path (TR-226)
    trigger: str = ""            # "" = no trigger of its own: only a handoff starts it
    prompt: str
    slack_webhook: str = ""      # legacy per-agent notification path (blank-to-keep on update)
    model: str = ""              # "" = the provider's default model
    provider: str = ""           # a provider id from Settings; "" = the cell default (TR-302)
    slack_channel: str = ""      # workspace-bot channel; the primary notification path
    webhook_url: str = ""        # write-back: findings + run metadata POSTed here
    webhook_token: str = ""      # optional bearer for the write-back (secret; blank-to-keep)
    webhook_key_label: str = ""  # the write-back reports this label's value as `key`; "" = the entity
    slack_webhook_clear: bool = False   # blank means keep (it is a secret), so clearing is explicit
    mcp_servers: list[str] = []  # registry names this agent may use
    max_rounds: int | None = None   # model rounds per run; None = default (6, or 12 with MCP servers)
    budget_usd: float | None = None  # lifetime spend cap in USD; None = no budget
    project: str = ""            # "" = the default project on create, unchanged on update
    # [{verdict, agent, cooldown}]: who takes over when a run concludes with that verdict
    # (TR-334); None = none on create, unchanged on update
    handoffs: list[dict] | None = None
    # how a run ends: always with the conclude tool, and the verdicts it may give
    # ([{verdict, when}]); None = off / none on create, unchanged on update
    concludes: bool | None = None
    verdicts: list[dict] | None = None
    # the GitHub credential the agent acts with (the check-run tool); None = none on create,
    # unchanged on update; "" clears. Reads and writes go through its GitHub MCP server.
    github: str | None = None


class GithubMcpIn(BaseModel):
    write: bool = False          # write toolsets (branches, files, PRs, comments, reviews)


class ImportReq(BaseModel):
    yaml: str
    mode: str = "merge"    # merge (upsert) | replace (clear catalog first)


class ProjectIn(BaseModel):
    template: str = ""
    name: str = ""         # defaults to the template title
    params: dict = {}
    objects: list | None = None   # template "custom": the objects, each {kind, name}
    goal: str | None = None       # one line, at most 200 characters; none = the template's GOAL

    @model_validator(mode="before")
    @classmethod
    def _accept_recipe(cls, data):
        # `recipe` is the pre-1.14 name of the field; accepted until two releases after 1.14
        if isinstance(data, dict) and not data.get("template") and data.get("recipe"):
            data = {**data, "template": data["recipe"]}
        return data


class ProjectUpdate(BaseModel):
    params: dict = {}
    name: str = ""         # blank = unchanged
    objects: list | None = None   # template "custom": the new object list
    # the project's goal; left out = unchanged, "" or null = cleared. A body that sets only the
    # goal changes nothing else, and is the one edit the default project takes.
    goal: str | None = None


class ProjectRepair(BaseModel):
    key: str               # a plan key (or "kind:key")


class McpServerIn(BaseModel):
    name: str
    url: str
    auth_header: str = ""        # header name; empty means Authorization when a value is set
    auth_value: str = ""         # the credential (secret; blank-to-keep on update), or
                                 # `credential:github/<name>` to use a stored GitHub credential
    headers: dict[str, str] = {}  # extra non-secret headers sent on every request
    project: str = ""            # "" = the default project on create, unchanged on update


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


class AskSessionIn(BaseModel):
    title: str = ""
    state: str             # opaque JSON blob owned by the console (messages + decisions)


class AgentLimitsIn(BaseModel):
    daily_cap: int | str | None = None   # "" or null clears the console value


class TracingIn(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    endpoint: str | None = None
    api_key: str | None = None
    headers: str | None = None


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


def _seed_project(store, projects) -> None:
    """Create TARES_SEED_PROJECT's project once, on the first boot that carries the var. The
    `seeded_project` settings marker (`seeded_usecase` before 1.14) is the one-shot record: it is written after the attempt
    (success or failure), so a user who deletes the seeded project never gets it back on a pod
    restart. An unknown template key writes NO marker — a config typo stays fixable by fixing the
    config. Never raises: a failed seed is a log line, not a dead cell."""
    if not SEED_PROJECT:
        return
    if store.get_setting("seeded_project") or store.get_setting("seeded_usecase"):
        return
    from .projects.registry import list_templates
    if SEED_PROJECT not in {r.key for r in list_templates()}:
        print(f"taresd: TARES_SEED_PROJECT names unknown template {SEED_PROJECT!r}; not seeded")
        return
    try:
        inst = projects.create(SEED_PROJECT, params={})
        print(f"taresd: seeded project {SEED_PROJECT!r} ({inst['id']})")
    except Exception as e:
        print(f"taresd: seeding project {SEED_PROJECT!r} failed: {type(e).__name__}: {e}")
    store.set_setting("seeded_project", SEED_PROJECT)


def make_app() -> FastAPI:
    try:
        store = Store(DB_PATH)
    except StoreUnavailable as e:
        # Losing the DB must not cost us the ability to SAY that we lost the DB. Log the full
        # traceback (degraded mode explains the failure to the user, it does not hide it from the
        # operator) and serve the console + 503s instead of exiting before uvicorn ever binds.
        traceback.print_exc()
        print(f"taresd: DEGRADED; {e.reason}. Serving the console and 503s; fix the database "
              f"and restart.", flush=True)
        return _degraded_app(e.reason)

    # seed the DB catalog from YAML on first boot — or on every boot when CATALOG_SYNC is set, so
    # the file stays the source of truth (the admin interface for a read-only demo: edit + restart).
    if Path(CATALOG_PATH).exists() and (store.catalog_empty() or CATALOG_SYNC):
        counts = import_yaml_to_db(store, Path(CATALOG_PATH).read_text(),
                                   engine=ProjectEngine(store))
        store.migrate_claude_code_repo_label()   # an old catalog file may still say `project`
        how = "synced" if CATALOG_SYNC else "imported"
        print(f"taresd: {how} {CATALOG_PATH} into catalog "
              f"({counts['sources']} sources, {counts['triggers']} triggers"
              f"{', ' + str(counts['projects']) + ' projects' if counts.get('projects') else ''})")

    dispatcher = Dispatcher(store)
    runtime = Runtime(store, dispatcher)
    # poll connectors hold off while ingest is paused for storage (see storage_state)
    runtime.storage_full = lambda: storage_state(store, DB_PATH)["detail"] if storage_state(store, DB_PATH)["paused"] else None
    # Projects: templates instantiated with params; they create and own ordinary catalog objects.
    projects = ProjectEngine(store, reload=runtime.reload_catalog, runtime=runtime)
    # Tares agents are the second kind of subscriber to a firing (the first is an external agent's
    # webhook). In-process, so `tares up` closes the loop with nothing to deploy.
    dispatcher.agents = AgentRunner(store, runtime)
    dispatcher.runtime = runtime
    # Agent tracing (tracing.py): the runner's provider cache, shared with Ask so both surfaces
    # follow the same switch and land in the same backend.
    tracing = dispatcher.agents.tracing

    # projects made from a template before goals existed get the template's goal (once)
    projects.fill_template_goals()
    # and the triggers a template planned get its plain-words description (once)
    filled = projects.fill_template_trigger_descriptions()
    # triggers on GitHub token sources keep meaning "a commit" now that those report PRs (once)
    filled += projects.fill_github_commit_filters()
    if filled:
        # re-read the catalog only: nothing runs yet (the sources start in lifespan), and a
        # reload that restarts sources needs a running event loop (it crashed 1.38.0-rc.2's first
        # start on a cell with template projects)
        runtime.catalog = catalog_from_db(store)
    _seed_project(store, projects)

    def _otlp_source_for(header: str | None) -> str:
        """Resolve the OTLP source for an export (shared by the HTTP and gRPC receivers). Raises
        KeyError/ValueError; auto-provisions a single `otlp` source on the first export."""
        names = [s.name for s in runtime.catalog.sources.values() if s.connector == "otlp"]
        if header:
            if header not in names:
                raise KeyError(f"unknown OTLP source {header!r}")
            return header
        if len(names) == 1:
            return names[0]
        if not names:   # zero-setup: provision one on first export (point a collector and go)
            store.upsert_catalog_source(
                "otlp", source_type_for("otlp"), "otlp", "5s",
                normalize_config("otlp", {"labels": [
                    {"name": "service", "field": "resourceAttributes.service.name",
                     "primary": True}]}))
            runtime.reload_catalog()
            print("taresd: auto-provisioned OTLP source 'otlp'")
            return "otlp"
        raise ValueError("multiple OTLP sources; set the X-Tares-Source header")

    @asynccontextmanager
    async def lifespan(_app):
        dispatcher.agents.attach_loop()
        runtime.start_all()
        # the daemon's own counters (/metrics, TR-308): storage gauges read on scrape, the event
        # loop watched by a one-second timer for the whole life of the process
        _metrics.set_storage_probe(lambda: {**store.usage(), **{
            k: v for k, v in storage_state(store, DB_PATH).items()
            if k in ("max_bytes", "pct_used", "paused")}})
        try:
            from importlib.metadata import version as _pkg_version
            _metrics.set_info(_pkg_version("tares"))
        except Exception:
            _metrics.set_info(None)
        _metrics.prime(providers=[p["id"] for p in providers_mod.list_providers(store)["providers"]])
        # providers the environment handed the cell list their models now, not on a click
        asyncio.create_task(providers_mod.discover_env_entries(store))
        loop_stop = asyncio.Event()
        loop_watch = asyncio.create_task(_metrics.watch_event_loop(loop_stop))
        # the clock for schedule triggers (TR-320)
        from . import schedule as _schedule
        schedule_task = asyncio.create_task(_schedule.run(runtime, loop_stop))
        print(f"taresd: {len(runtime.catalog.sources)} source(s); "
              f"console at / · agent API at /read · management API at /api")
        # optional OTLP gRPC receiver (:4317). Needs grpcio + opentelemetry-proto; off if absent.
        grpc_server = None
        port = os.getenv("TARES_OTLP_GRPC_PORT", "4317")
        if port and port.lower() not in ("off", "none", "0"):
            try:
                from .otlp_grpc import serve as _serve_otlp_grpc
                grpc_server = await _serve_otlp_grpc(int(port), _otlp_source_for, runtime.ingest_otlp)
                print(f"taresd: OTLP gRPC receiver on :{port}")
            except ImportError:
                print("taresd: OTLP gRPC off (install tares[otlp-grpc] to enable)")
            except Exception as e:   # never let the optional receiver block startup
                print(f"taresd: OTLP gRPC failed to start: {e}")
        yield
        loop_stop.set()
        loop_watch.cancel()
        schedule_task.cancel()
        if grpc_server is not None:
            await grpc_server.stop(grace=2)
        runtime.shutdown()
        tracing.shutdown()   # flush the last spans; a lost trace is not a lost run

    app = FastAPI(title="taresd", lifespan=lifespan)
    app.state.store = store   # for in-process tests; DuckDB is single-writer, so no second Store
    app.state.runtime = runtime
    app.state.agents = dispatcher.agents   # the Tares agent runner, for in-process tests
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    # ── auth: scoped credentials ──────────────────────────────────────────────
    # Three scopes — read (consume: reads, catalog reads, subscribe), ingest (contribute:
    # /ingest, /v1/*, remember), admin (configure: catalog CRUD, discover, credentials, keys).
    # Credentials: the env AUTH_TOKEN is the implicit root (admin, non-revocable), plus revocable
    # scoped keys in the api_keys table (docs/design/api-keys.md).
    _ADMIN_PATHS = ("/api/catalog/export", "/api/catalog/import", "/api/agent/chat")
    # the only writes a read key may make: reads that take a body, and a reader's own delivery.
    # Every other write, including a route added later, needs admin.
    _READ_WRITES = ("/read", "/subscribe", "/unsubscribe", "/api/labels/preview")
    # a project's skills: what its agents are told to do, so writing one is a catalog write
    _SKILLS_PATH = re.compile(r"^/api/projects/[^/]+/skills(/|$)")

    def _required_scope(method: str, path: str) -> str | None:
        """None = public. 'any' = any valid credential. Reads of credentials and every write are
        admin, except the few in _READ_WRITES: a read with a body, and subscribe, which exposes
        nothing a reader couldn't pull and forward; it only persists that reader's own delivery."""
        if (method == "POST" and path.startswith("/ingest/")
                and runtime.signature_for(path[len("/ingest/"):])):
            # a source that checks signatures (a GitHub App, a signed webhook) is authenticated by
            # the signature the route verifies on the raw body; GitHub cannot send a Tares key
            return None
        if method == "POST" and (_is_ingest(path) or path == "/remember"):
            return "ingest"   # before _public(): ingest paths are "public" only in the sense of
                              # not needing the auth token — they have their own scope
        if _public(method, path):
            return None
        if path == "/api/whoami":
            return "any"
        # /api/settings holds instance credentials (the Anthropic key): admin even to READ, since
        # a read tells you whether and where a credential is configured.
        if path == "/api/setup/github-suggestion":
            return "admin"   # spends model money and distills private READMEs: not for read keys
        if (path in _ADMIN_PATHS or path.startswith("/api/keys")
                or path.startswith("/api/discover") or path.startswith("/api/settings")
                or path.startswith("/api/linear")):
            return "admin"
        if method != "GET" and (path.startswith("/api/sources")
                                or path.startswith("/api/triggers")
                                or path.startswith("/api/agents")
                                or _SKILLS_PATH.match(path)):
            return "admin"
        m = _PROJECT_PATH.match(path)
        if m:
            rest = m.group(2) or ""
            if method == "POST" and rest == "/findings":
                return "findings"   # recording one; admin implies it, a plain read key does not
            # a project's keys, and who is subscribed to it with which URL: credentials
            # ... and the guided setup, whose plan and checks describe the whole configuration
            if (rest.startswith("/keys") or rest.startswith("/subscribe")
                    or rest == "/external-agents" or rest.startswith("/setup")):
                return "admin"
            if method == "POST" and rest == "/stats":
                return "read"
        if method not in ("GET", "HEAD") and path not in _READ_WRITES:
            return "admin"
        return "read"

    # ── project keys (TR-335): what a key that belongs to one project may do ──
    # An allowlist, not a denylist: a route that is not named here answers 403 for a project key,
    # so a route added later is closed to project keys until someone decides otherwise. Each entry
    # is the scope the key needs; the handlers narrow what the allowed routes return to the key's
    # project (request.state.project_key).
    _PROJECT_PATH = re.compile(r"^/api/projects/([^/]+)(/.*)?$")
    _PK_ROUTES = {("GET", "/api/whoami"): "any", ("POST", "/read"): "read",
                  ("GET", "/catalog"): "read", ("GET", "/api/projects"): "read"}
    _PK_PROJECT_ROUTES = [   # (method, the path after /api/projects/<its id>, scope)
        ("GET", re.compile(r"^$"), "read"),
        ("GET", re.compile(r"^/timeline$"), "read"),
        ("GET", re.compile(r"^/skills$"), "read"),
        ("GET", re.compile(r"^/skills/[^/]+$"), "read"),
        ("GET", re.compile(r"^/findings$"), "read"),
        ("GET", re.compile(r"^/docs$"), "read"),
        ("GET", re.compile(r"^/docs/[^/]+$"), "read"),
        ("GET", re.compile(r"^/tickets$"), "read"),
        ("GET", re.compile(r"^/tickets/[^/]+$"), "read"),
        ("GET", re.compile(r"^/sessions$"), "read"),
        ("GET", re.compile(r"^/sessions/[^/]+$"), "read"),
        ("GET", re.compile(r"^/results$"), "read"),
        ("GET", re.compile(r"^/results/[^/]+$"), "read"),
        ("GET", re.compile(r"^/outline$"), "read"),
        ("GET", re.compile(r"^/health$"), "read"),
        ("POST", re.compile(r"^/findings$"), "findings"),
        ("POST", re.compile(r"^/stats$"), "read"),
        ("POST", re.compile(r"^/subscribe$"), "read"),
        ("DELETE", re.compile(r"^/subscribe/[^/]+$"), "read"),
    ]

    def _project_key_scope(method: str, path: str, project: str) -> str | None:
        """The scope a project key needs for this request, or None when a project key may not
        make it at all (any other route, or another project's)."""
        if (method, path) in _PK_ROUTES:
            return _PK_ROUTES[(method, path)]
        if method == "GET" and path.startswith("/catalog/"):
            return "read"   # the handler allows only the project's own sources and triggers
        m = _PROJECT_PATH.match(path)
        if m and m.group(1) == project:
            rest = m.group(2) or ""
            for meth, pattern, scope in _PK_PROJECT_ROUTES:
                if meth == method and pattern.match(rest):
                    return scope
        return None

    def _project_key_denied(project: str) -> str:
        p = store.get_project(project)
        return (f"this key only reads project {p['name'] if p else project} and records "
                "findings in it")

    def _pk(request: Request) -> str | None:
        """The project of the request's project key, or None for any other credential (and on an
        open instance, where no key is checked)."""
        return getattr(request.state, "project_key", None)

    def _resolve_credential(request) -> tuple[set, dict] | tuple[None, None]:
        """Token from the request -> (scopes, identity), or (None, None) if unknown/absent."""
        tok = _bearer(request.headers.get("authorization")) or request.headers.get("x-tares-token", "")
        if not tok:
            return None, None
        if AUTH_TOKEN and tok == AUTH_TOKEN:
            return {"read", "ingest", "admin"}, {"id": "env:auth", "name": "auth token (env)"}
        key = store.find_api_key(hashlib.sha256(tok.encode()).hexdigest())
        if key:
            last = key.get("last_used_at")
            if last is None or (now_utc() - last).total_seconds() > 60:   # throttle write churn
                store.touch_api_key(key["id"])
            ident = {"id": f"key:{key['id']}", "name": key["name"]}
            if key.get("project"):
                ident["project"] = key["project"]
            return set(key["scopes"]), ident
        return None, None

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
            return await call_next(request)

    def _err(e: Exception, code: int = 400):
        raise HTTPException(status_code=code, detail=str(e))

    # ── agent surface (unchanged contract; queries now logged) ───────────────
    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint():
        """The daemon's own counters in the Prometheus text format (TR-308): events per source,
        poll outcomes, source state, agent runs, model calls and spend by provider, storage against
        its limit, trigger evaluation time, event loop lag. Public like /health; counts only."""
        try:
            body, ctype = _metrics.render()
        except RuntimeError:
            _err(KeyError("metrics need the prometheus-client package"), 404)
        return Response(content=body, media_type=ctype)

    @app.get("/health")
    async def health():
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
        body = {"status": status, "auth_required": bool(AUTH_TOKEN),
                "sources": [] if AUTH_TOKEN else list(runtime.catalog.sources),
                "pct_used": pct, "version": _installed_version(),
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
            return store.default_project_id() if default else None
        if store.get_project(ref) is not None:
            return ref
        p = store.get_project_by_name(ref)
        if p is None:
            _err(ValueError(f"unknown project {ref!r}"), 400)
        return p["id"]

    _SHARED_CONNECTORS = ("finding", "memory")

    def _project_view(uid: str) -> tuple[list[str], dict]:
        """What a project reads: its member sources plus the shared findings and memory sources,
        and the row scope that narrows those shared ones to the project's own rows (findings of
        its agents and findings recorded in it)."""
        shared = sorted(n for n, c in runtime.catalog.sources.items()
                        if c.connector in _SHARED_CONNECTORS)
        members = [s for s in store.project_sources(uid) if s in runtime.catalog.sources]
        agents = [a["name"] for a in store.list_catalog_agents()
                  if uid in store.projects_using("agent", a["name"])]
        return (sorted(set(members) | set(shared)),
                {"sources": shared, "project": uid, "agents": agents})

    def _key_project(request: Request, ref: str) -> str | None:
        """The project a read is narrowed to: a project key's own (naming another answers 403),
        else the one named, else None."""
        pk = _pk(request)
        if pk:
            p = store.get_project(pk) or {}
            if ref and ref.strip() not in (pk, p.get("name")):
                _err(PermissionError(_project_key_denied(pk)), 403)
            return pk
        return _resolve_project(ref) if ref else None

    @app.post("/read")
    async def read(req: ReadReq, request: Request):
        """Raw label-native read. The selector is a {label: value} conjunction (strict AND). By
        default it reads every source; `project` narrows it to that project's sources (the
        shared findings and memory sources included, with only the project's own rows) and
        `sources` to the ones named (both: the named ones within the project). A project key
        always reads its own project."""
        if not req.selector:
            _err(ValueError('read needs a selector, e.g. {"project": "frontend"}'))
        names, scope = None, None
        uid = _key_project(request, req.project)
        if uid:
            names, scope = _project_view(uid)
        if req.sources:
            if _pk(request):
                # a project key learns nothing about sources outside its project, not even
                # whether they exist
                if set(req.sources) - set(names or []):
                    _err(PermissionError(_project_key_denied(uid)), 403)
            unknown = sorted(set(req.sources) - set(runtime.catalog.sources))
            if unknown:
                _err(KeyError(f"unknown sources {unknown}"), 404)
            names = [s for s in req.sources if names is None or s in names]
        payload, nrows, sources, rows = resolve_read(store, runtime.catalog, req.selector, req.window,
                                                     include_payload=req.include_payload,
                                                     sources=names, scope=scope)
        log_key = ", ".join(f"{k}={v}" for k, v in req.selector.items())
        store.log_query("r_" + uuid.uuid4().hex[:12], "(read)", log_key, req.window,
                        nrows, req.client)
        return {"payload": payload, "count": nrows, "sources": sources, "rows": rows}

    # Views were removed: a trigger names its own sources. The old routes answer 404 with the
    # reason instead of the generic "unknown path", so an old client learns what changed.
    def _views_removed():
        _err(KeyError(VIEWS_REMOVED), 404)

    for _gone in ("/query", "/derive", "/api/views", "/api/views/{name}"):
        app.add_api_route(_gone, _views_removed, methods=["GET", "POST", "PUT", "DELETE"],
                          include_in_schema=False)

    @app.post("/subscribe")
    async def subscribe(req: SubReq, request: Request):
        if req.trigger not in {t.name for t in runtime.catalog.triggers}:
            _err(KeyError(f"unknown trigger {req.trigger!r}"), 404)
        url = req.url.strip()
        if url.startswith(SLACK_URL_PREFIX):
            # A Slack subscription is checked at creation, not at the first firing: an unroutable
            # channel or a missing token would otherwise surface hours later as a failed delivery
            # nobody is watching.
            try:
                url = slack_url(validate_slack_channel(url[len(SLACK_URL_PREFIX):]))
            except ValueError as e:
                _err(e)
            if not resolve_slack_token(store)[0]:
                _err(ValueError("no Slack bot token configured; set TARES_SLACK_BOT_TOKEN or "
                                "add one under Settings before subscribing a channel"))
        sid = "sub_" + uuid.uuid4().hex[:8]
        # record the creating credential: revoking a key removes its subscriptions (a revoked
        # agent must stop receiving trigger dispatches)
        ident = getattr(request.state, "credential", None)
        store.add_subscription(sid, req.trigger, url, created_by=ident["id"] if ident else None)
        return {"subscription_id": sid}

    @app.post("/unsubscribe")
    async def unsubscribe(req: UnsubReq):
        store.remove_subscription(req.subscription_id)
        return {"ok": True}

    @app.get("/catalog")
    async def catalog_list(request: Request):
        members = store.source_memberships()
        pk = _pk(request)
        if pk:   # a project key sees its own project only
            names, _scope = _project_view(pk)
            p = store.get_project(pk) or {}
            return {
                "sources": [{"name": n, "type": runtime.catalog.sources[n].type,
                             "projects": [pk]} for n in names],
                "triggers": [{"name": t.name, "project": t.project, "sources": t.sources,
                              "key_field": t.key_field}
                             for t in runtime.catalog.triggers if pk in store.projects_using("trigger", t.name)],
                "projects": [{"id": pk, "name": p.get("name"), "template": p.get("template")}],
            }
        return {
            "sources": [{"name": s.name, "type": s.type, "projects": members.get(s.name, [])}
                        for s in runtime.catalog.sources.values()],
            "triggers": [{"name": t.name, "project": t.project, "sources": t.sources,
                          "key_field": t.key_field} for t in runtime.catalog.triggers],
            "projects": [{"id": p["id"], "name": p["name"], "template": p["template"]}
                         for p in store.list_projects()],
        }

    # ── catalog.describe — the discovery surface (design doc §4 MCP surface) ──
    def _lineage_edges() -> list[dict]:
        edges = []
        for t in runtime.catalog.triggers:
            for s in t.sources:
                edges.append({"from": f"source:{s}", "to": f"trigger:{t.name}",
                              "transform": "condition"})
        return edges

    def _lag_seconds(ts) -> float | None:
        if ts is None:
            return None
        aware = ts if ts.tzinfo else ts.replace(tzinfo=now_utc().tzinfo)
        return max((now_utc() - aware).total_seconds(), 0.0)

    def _source_label_names(entry: dict) -> list[str]:
        cfg = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        return [l["name"] for l in (cfg.get("labels") or []) if isinstance(l, dict) and l.get("name")]

    def _source_primary(entry: dict) -> str | None:
        """Name of the source's explicitly-marked primary label (the key), or None."""
        cfg = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        for l in cfg.get("labels") or []:
            if isinstance(l, dict) and l.get("primary"):
                return l.get("name")
        return None

    def _label_facets() -> dict:
        """Entity axes the console facets by. A key is just the label marked primary, so facets
        are named labels (one flagged primary). Sources with no declared labels fall back to an
        unnamed `key` facet (their bare key_value)."""
        facets: dict = {}
        for s in store.list_catalog_sources():
            names = _source_label_names(s)
            if names:
                primary = _source_primary(s)
                for ln in names:
                    f = facets.setdefault(ln, {"sources": [], "primary": False})
                    f["sources"].append(s["name"])
                    if ln == primary:
                        f["primary"] = True
            else:
                f = facets.setdefault("key", {"sources": [], "primary": True, "unnamed": True})
                f["sources"].append(s["name"])
        return facets

    @app.get("/catalog/{handle}")
    async def catalog_describe(handle: str, request: Request):
        kind, _, name = handle.partition(":")
        pk = _pk(request)
        if pk:
            # a project key describes its project's own sources and triggers; the shared findings
            # and memory sources hold other projects' rows too, so they are read, not described
            mine = ({t.name for t in runtime.catalog.triggers if pk in store.projects_using("trigger", t.name)}
                    if kind == "trigger" else
                    {s for s in store.project_sources(pk)
                     if s in runtime.catalog.sources
                     and runtime.catalog.sources[s].connector not in _SHARED_CONNECTORS})
            if kind not in ("source", "trigger") or name not in mine:
                _err(PermissionError(_project_key_denied(pk)), 403)
        if kind == "view":
            _err(KeyError(VIEWS_REMOVED), 404)
        if not name or kind not in ("source", "trigger"):
            _err(ValueError("handle must be source:<name> or trigger:<name>"))
        edges = [e for e in _lineage_edges() if handle in (e["from"], e["to"])]
        if pk:
            project_triggers = {f"trigger:{t.name}" for t in runtime.catalog.triggers
                                if pk in store.projects_using("trigger", t.name)}
            edges = [e for e in edges if e["to"] in project_triggers]

        if kind == "source":
            entry = next((s for s in store.list_catalog_sources() if s["name"] == name), None)
            if entry is None:
                _err(KeyError(f"unknown source {name!r}"), 404)
            entry = {**entry, "config": redact_config(entry["connector"], entry["config"])}
            health = runtime.health_snapshot().get(name) or {}
            names = _source_label_names(entry)
            if names:
                labels = {ln: store.list_entities(ln, sources=[name], limit=20) for ln in names}
                primary_label = _source_primary(entry)
            else:
                labels = {"key": store.list_entities("key_value", sources=[name], limit=20)}
                primary_label = "key"
            return {
                "handle": handle, "kind": "source", "entry": entry,
                "schema": store.source_schema(name),
                "labels": labels,           # each named axis with its observed values
                "primary_label": primary_label,   # which axis is the key
                "freshness": {"last_event_time": health.get("last_ingest"),
                              "lag_seconds": _lag_seconds(health.get("last_ingest")),
                              "events_total": health.get("events_total", 0),
                              "status": health.get("status")},
                "lineage": edges,
                "sample": store.recent_events(source=name, limit=3),
            }

        entry = next((t for t in store.list_catalog_triggers() if t["name"] == name), None)
        if entry is None:
            _err(KeyError(f"unknown trigger {name!r}"), 404)
        return {
            "handle": handle, "kind": "trigger", "entry": entry,
            "subscribers": len(store.list_subscriptions(name)),
            "lineage": edges,
        }

    # ── entities — the (label, value) pairs, faceted (the entity surface) ─────
    @app.get("/api/entities")
    async def entities(label: str | None = None, limit: int = 50):
        facets = _label_facets()

        def values_for(name: str, facet: dict, lim: int):
            # an unnamed `key` facet reads the bare key_value, scoped to its label-less sources;
            # a named label reads that label across events that carry it
            if facet.get("unnamed"):
                return store.list_entities("key_value", sources=facet["sources"], limit=lim)
            return store.list_entities(name, limit=lim)

        def entry(name: str, facet: dict, lim: int) -> dict:
            srcs = facet["sources"] if facet.get("unnamed") else None
            lbl = "key_value" if facet.get("unnamed") else name
            return {"label": name, "primary": facet.get("primary", False),
                    "sources": sorted(set(facet["sources"])),
                    # high-cardinality: exceeded the cap, so it's served by a live scan, not the
                    # counter — surface it so the UI can flag it as "not a useful entity axis".
                    "high_cardinality": store.is_label_truncated(lbl, srcs),
                    "values": values_for(name, facet, lim)}

        if label is not None:
            if label not in facets:
                _err(KeyError(f"unknown label {label!r} (have {sorted(facets)})"), 404)
            return entry(label, facets[label], min(limit, 500))
        return {"labels": [entry(ln, f, min(limit, 50)) for ln, f in facets.items()]}

    # ── remember — the agent writes its own memory back (closes the loop) ─────
    @app.post("/remember", status_code=202)
    async def remember(req: RememberReq):
        source_name = req.source or next(
            (s.name for s in runtime.catalog.sources.values() if s.connector == "memory"), None)
        if source_name is None:   # first memory ever: provision the source on the fly
            source_name = "agent_memory"
            store.upsert_catalog_source(source_name, "agent_memory", "memory", "5s", {})
            runtime.reload_catalog()
            print(f"taresd: auto-provisioned memory source {source_name!r}")
        payload = {"key": req.key, "content": req.content,
                   "memory_type": req.memory_type, "fields": req.fields}
        try:
            n = await runtime.ingest(source_name, payload)
        except KeyError as e:
            _err(e, 404)
        except ValueError as e:
            _err(e)
        return {"ok": True, "source": source_name, "ingested": n}

    # ── push ingestion (webhook sources) ──────────────────────────────────────
    def _verify_headers(request: Request) -> dict:
        """Vercel verifies a drain endpoint via the x-vercel-verify response header — echo back the
        value it sends on its verification probe (how the Vercel drain flow actually verifies)."""
        val = request.headers.get("x-vercel-verify")
        return {"x-vercel-verify": val} if val else {}

    async def _parse_ingest_body(request: Request):
        """Accept a JSON object/array OR NDJSON (one JSON object per line, as some log drains send).
        An empty body (a verification ping) parses to []."""
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

    @app.get("/ingest/{token}", include_in_schema=False)
    async def ingest_verify(token: str, request: Request):
        # Vercel (and similar) probe the endpoint before saving a drain — answer with the verify header.
        return JSONResponse({"ok": True}, headers=_verify_headers(request))

    def _refuse_if_full() -> None:
        """507 Insufficient Storage when the store is at the pause mark: the producer gets a plain
        reason and can retry later; the database keeps working for everything else."""
        st = storage_state(store, DB_PATH)
        if st["paused"]:
            raise HTTPException(status_code=507, detail=st["detail"])

    @app.post("/ingest/{token}", status_code=202)
    async def ingest(token: str, request: Request):
        """Push events into a source, as a JSON object, a JSON array or NDJSON.

        `X-Tares-Bypass-Cooldown: true` (or `1`) marks the delivery on-demand: every trigger over
        this source fires for the keys these events carry even when those keys are inside their
        cooldown, for a person asking for this one analysis now. It is not a switch that turns the
        cooldown off. The firing records its time as usual, so the next unmarked event for the key
        waits the full cooldown again. The header carries no authority of its own: ingest still
        needs a credential with the `ingest` scope.
        """
        _refuse_if_full()
        sig = runtime.signature_for(token)
        if sig:
            # verified on the exact bytes, before parsing (re-serialized JSON would not match)
            from .webhook_verify import verify
            reason = verify(sig["scheme"], sig["secret"], await request.body(),
                            dict(request.headers), sig.get("header"))
            if reason:
                runtime.count_rejected(sig["source"], reason)
                return JSONResponse({"detail": f"signature check failed ({sig['scheme']}): "
                                               f"{reason}"}, status_code=401)
        body = await _parse_ingest_body(request)
        try:
            n = await runtime.ingest(token, body, bypass_cooldown=_bypass_cooldown(request),
                                     headers=dict(request.headers))
        except KeyError as e:
            _err(e, 404)
        except ValueError as e:
            _err(e, 400)
        await _seed_challenger(token, body)
        return JSONResponse({"ingested": n}, status_code=202, headers=_verify_headers(request))

    _seed_lock = asyncio.Lock()

    async def _seed_challenger(token: str, body) -> None:
        """The first marked session creates the challenger_workflow project (trigger,
        summarizer). Best effort: an ingest never fails because of it."""
        from .projects.challenger_workflow import ensure_instance, has_challenger_line
        if not has_challenger_line(body):
            return
        cfg = runtime.catalog.sources.get(token) or next(
            (c for c in runtime.catalog.sources.values() if c.ingest_key == token), None)
        if cfg is None or cfg.connector != "claude_code":
            return
        async with _seed_lock:
            try:
                uid = ensure_instance(projects)   # inline, like the create route: reload needs the loop
            except Exception as e:
                print(f"taresd: could not create the challenger workflow project: {e}")
                return
        if uid:
            print(f"taresd: created the challenger workflow project {uid} (first challenger session)")

    # ── OTLP receiver (OpenTelemetry/HTTP JSON; gRPC counterpart in lifespan) ──
    def _resolve_otlp_source(header: str | None) -> str:
        try:
            return _otlp_source_for(header)   # shared with the gRPC receiver
        except KeyError as e:
            _err(e, 404)
        except ValueError as e:
            _err(e)

    async def _otlp(signal: str, request: Request):
        _refuse_if_full()
        try:
            body = await request.json()
        except Exception:
            _err(ValueError("invalid JSON (OTLP/HTTP JSON expected)"))
        source = _resolve_otlp_source(request.headers.get("x-tares-source"))
        try:
            await runtime.ingest_otlp(source, signal, body)
        except KeyError as e:
            _err(e, 404)
        except ValueError as e:
            _err(e, 400)
        return {}   # OTLP success: an (empty) ExportServiceResponse

    @app.post("/v1/logs")
    async def otlp_logs(request: Request):
        return await _otlp("logs", request)

    @app.post("/v1/traces")
    async def otlp_traces(request: Request):
        return await _otlp("traces", request)

    @app.post("/v1/metrics")
    async def otlp_metrics(request: Request):
        return await _otlp("metrics", request)

    # ── management API: connectors ────────────────────────────────────────────
    @app.get("/api/connectors")
    async def connectors():
        return {name: spec for name, spec in SPECS.items()}

    # (there is no /api/security — whether auth is on is a boolean on /health; producers authenticate
    #  with scoped ingest API keys, not a shared token.)

    # ── API keys: scoped, revocable credentials (admin scope; see docs/design) ─
    _SCOPES = {"read", "ingest", "admin"}

    @app.get("/api/keys")
    async def list_keys():
        return {"keys": store.list_api_keys(),
                "enforced": bool(AUTH_TOKEN),   # without a root auth token the instance is open
                "scopes": sorted(_SCOPES)}

    # A project key (TR-335) reads one project and records findings in it; nothing else.
    _PROJECT_SCOPES = {"read", "findings"}

    def _make_key(name: str, scopes: list[str], project: str | None) -> dict:
        kid = uuid.uuid4().hex[:8]
        secret = f"nvf_{kid}_{secrets.token_urlsafe(24)}"
        store.insert_api_key(kid, name, f"nvf_{kid}", hashlib.sha256(secret.encode()).hexdigest(),
                             scopes, project=project)
        # the secret exists only in this response; the store keeps its hash
        out = {"id": kid, "name": name, "scopes": scopes, "secret": secret}
        if project:
            out["project"] = project
        return out

    @app.post("/api/keys", status_code=201)
    async def create_key(body: dict = Body(...)):
        """{name, scopes, project?}. With `project` (an id or a name) it is a project key: scopes
        `read` and `findings` (the default), over that project only."""
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
            return _make_key(name, scopes, uid)
        if "findings" in scopes:
            _err(ValueError("the findings scope is for a project key; name the project"))
        if not scopes or not set(scopes) <= _SCOPES:
            _err(ValueError(f"scopes must be a non-empty subset of {sorted(_SCOPES)}"))
        return _make_key(name, scopes, None)

    @app.delete("/api/keys/{kid}")
    async def revoke_key(kid: str):
        if not store.revoke_api_key(kid):
            _err(KeyError(f"no active key {kid!r}"), 404)
        return {"ok": True, "note": "key revoked; its subscriptions were removed"}

    @app.get("/api/whoami")
    async def whoami(request: Request):
        ident = getattr(request.state, "credential", None)
        scopes = getattr(request.state, "scopes", None)
        if ident is None:   # guard not active (open instance) or ingest-path credential
            return {"id": "open", "name": "no auth configured", "scopes": sorted(_SCOPES)}
        out = {**ident, "scopes": scopes or []}
        if ident.get("project"):   # a project key: the project it reads, by id and name
            out["project_name"] = (store.get_project(ident["project"]) or {}).get("name")
        return out

    @app.get("/api/capabilities")
    async def capabilities():
        """Host capabilities that gate local-only console features, so the UI can hide actions that
        can't work on this deployment — e.g. a hosted cell has no Docker socket, so Auto-discover
        would be a dead end. (Claude Code is plugin-based and works everywhere, so it's not gated.)"""
        import shutil
        from importlib.metadata import version as _pkg_version
        try:
            ver = _pkg_version("tares")   # the installed release (release.sh bumps pyproject.toml)
        except Exception:
            ver = None
        url_configured = resolve_api_base(store)[1] != ""
        return {
            "version": ver,
            "discover_docker": shutil.which("docker") is not None or os.path.exists("/var/run/docker.sock"),
            # the Ask assistant runs on the same resolved key as Tares agents (env ANTHROPIC_API_KEY
            # or the console-stored key) — so once a key is set anywhere, the Ask chat stops
            # prompting for a browser-pasted one.
            "agent_key_configured": resolve_provider(store)[0] is not None,
            # Ask and the builder run on the cell default; the chat says which
            "default_provider": next(({"id": p["id"], "name": p["name"], "kind": p["kind"]}
                                      for p in providers_mod.list_providers(store)["providers"]
                                      if p["default"]), None),
            "url_configured": url_configured,
            # gates the "subscribe a Slack channel" affordance on a trigger — offering it with no
            # bot token configured only leads to a 400.
            "slack_configured": bool(resolve_slack_token(store)[0]),
        }

    @app.post("/api/labels/preview")
    async def label_preview(body: dict = Body(...)):
        """Pure preview of a label spec's value normalization against a source's OBSERVED values:
        before -> after with event counts, so the user sees the merge they're about to create
        before saving (docs/design/label-value-normalization.md). No state is touched."""
        from collections import Counter

        from .config import extract_labels
        from .connectors import build_connector, normalize_label_specs

        source = body.get("source")
        spec = body.get("label") or {}
        cfg = runtime.catalog.sources.get(str(source))
        if cfg is None:
            _err(KeyError(f"unknown source {source!r}"), 404)
        try:   # validate the label spec exactly like a save would (bad regex -> 400 here)
            normalized = normalize_label_specs([spec])
        except CatalogError as e:
            _err(e)
        if not normalized or "field" not in normalized[0]:
            _err(ValueError("preview needs a field label spec"))
        lspec = normalized[0]

        conn = build_connector(cfg, store)
        pairs: Counter = Counter()   # (before, after) -> events
        sampled = 0
        for payload in store.recent_payloads(cfg.name, 2000):
            ctx = conn.label_context(payload)
            if not isinstance(ctx, dict):
                continue
            sampled += 1
            plain = extract_labels([{k: v for k, v in lspec.items() if k not in ("pattern", "replace", "map")}], ctx)
            if lspec["name"] not in plain:
                continue
            before = plain[lspec["name"]]
            after = extract_labels([lspec], ctx)[lspec["name"]]
            pairs[(before, after)] += 1
        results = [{"from": b, "to": a, "events": n}
                   for (b, a), n in sorted(pairs.items(), key=lambda kv: -kv[1])]
        return {"sampled": sampled, "results": results,
                "distinct_before": len({b for b, _ in pairs}),
                "distinct_after": len({a for _, a in pairs})}

    @app.get("/api/sources/discover", include_in_schema=False)
    async def discover_source_get():
        _err(ValueError("discover is POST-only: POST /api/sources/discover "
                        "{connector, config}"), 405)

    @app.post("/api/sources/discover")
    async def discover_source(body: dict = Body(...)):
        from .connectors import REGISTRY
        connector = body.get("connector")
        if connector not in REGISTRY:
            _err(ValueError(f"unknown connector {connector!r}"), 404)
        config = dict(body.get("config", {}) or {})
        if config.get("credential") and not config.get("token"):
            # discover() is a classmethod with no store: resolve the stored GitHub credential
            # here so it can authenticate; the proposal keeps the reference, never the token
            from .github_credentials import get_token, resolve_api_url
            try:
                config["token"] = await get_token(store, config["credential"], config.get("repo"))
            except ValueError as e:
                _err(e, 404)
            config.setdefault("api_url", resolve_api_url(store, config["credential"]) or "")
        try:
            # bounded + catch-all: driver errors (asyncpg timeouts/auth/network) are not ValueError
            # and would otherwise 500 with nothing shown to the user
            proposal = await asyncio.wait_for(
                REGISTRY[connector].discover(config), timeout=30)
        except (TimeoutError, asyncio.TimeoutError):
            _err(ValueError("discover timed out; is the target reachable from the Tares "
                            "server? (a hosted Tares can only reach public endpoints)"))
        except HTTPException:
            raise
        except Exception as e:
            _err(ValueError(f"discover failed: {e}"))
        if proposal is None:
            _err(ValueError(f"connector {connector!r} doesn't support discovery yet"))
        return proposal

    @app.get("/api/discover/environment")
    async def discover_environment(provider: str = "docker"):
        if provider != "docker":
            _err(ValueError(f"unknown provider {provider!r} (have: docker)"), 404)
        from .discovery import scan_docker
        try:
            return await scan_docker()
        except ValueError as e:
            _err(e)


    # ── management API: sources ───────────────────────────────────────────────
    @app.get("/api/sources")
    async def list_sources():
        health = runtime.health_snapshot()
        members = store.source_memberships()
        return [{**s, "config": redact_config(s["connector"], s["config"]),
                 "projects": members.get(s["name"], []),
                 "health": health.get(s["name"])} for s in store.list_catalog_sources()]

    @app.get("/api/sources/{name}")
    async def get_source(name: str):
        for s in store.list_catalog_sources():
            if s["name"] == name:
                return {**s, "config": redact_config(s["connector"], s["config"]),
                        "projects": store.source_memberships().get(name, []),
                        "health": runtime.health_snapshot().get(name)}
        _err(KeyError(f"unknown source {name!r}"), 404)

    @app.post("/api/sources", status_code=201)
    async def create_source(body: SourceIn):
        if body.name in runtime.catalog.sources:
            _err(ValueError(f"source {body.name!r} already exists"), 409)
        uid = _resolve_project(body.project)
        try:
            validate_source_dict(body.model_dump())
            config = normalize_config(body.connector, body.config)
        except CatalogError as e:
            _err(e)
        store.upsert_catalog_source(body.name, source_type_for(body.connector), body.connector,
                                    body.poll, config)
        store.put_in_project("source", body.name, uid, creator=True)
        runtime.reload_catalog()
        cfg = runtime.catalog.sources.get(body.name)
        return {"ok": True, "name": body.name, "ingest_key": cfg.ingest_key if cfg else None}

    @app.put("/api/sources/{name}")
    async def update_source(name: str, body: SourceIn):
        existing = {s["name"]: s for s in store.list_catalog_sources()}.get(name)
        if existing is None:
            _err(KeyError(f"unknown source {name!r}"), 404)
        if body.name != name:
            _err(ValueError("renaming a source is not supported; delete and recreate"), 400)
        try:
            validate_source_dict(body.model_dump())
            # A client edits with the secret masked; if it saves the placeholder back unchanged,
            # keep the stored secret instead of overwriting it (see connectors.redact_config).
            config = normalize_config(
                body.connector, restore_secrets(body.connector, body.config, existing["config"]))
        except CatalogError as e:
            _err(e)
        store.upsert_catalog_source(name, source_type_for(body.connector), body.connector,
                                    body.poll, config, paused=existing["paused"])
        store.mark_customized("source", name)
        runtime.reload_catalog()
        # Label specs apply to NEW events going forward — ingest reads the current specs. Existing
        # events keep the labels they were ingested with. A retroactive relabel of stored events can
        # rewrite millions of rows, so it is a planned explicit background action (see
        # store.backfill_labels for the building block), never something that runs inline on an edit.
        return {"ok": True, "relabeled": False}

    # What else goes if an object is deleted, in delete order (agents, then triggers). The
    # delete dialogs show it and offer to take it along; the cascade deletes use the same list.
    @app.get("/api/catalog/dependents")
    async def catalog_dependents(kind: str, name: str):
        from .projects.engine import dependents
        if kind not in ("source", "trigger"):
            _err(ValueError("kind must be source or trigger"), 400)
        return {"dependents": dependents(store, kind, name)}

    def _delete_dependents(kind: str, name: str) -> list[str]:
        from .projects.engine import dependents
        gone = []
        for d in dependents(store, kind, name):
            if d["kind"] == "agent":
                store.delete_catalog_agent(d["name"])   # with its wiring in every project
            elif d["kind"] == "trigger":
                store.delete_catalog_trigger(d["name"])
                store.remove_subscriptions_by_trigger(d["name"])
            gone.append(f"{d['kind']}:{d['name']}")
        return gone

    @app.delete("/api/sources/{name}")
    async def delete_source(name: str, purge_events: bool = False, cascade: bool = False):
        """`cascade` takes the triggers that read this source and those triggers' agents with
        it; without it a source that something depends on is refused by name."""
        if name not in {s["name"] for s in store.list_catalog_sources()}:
            _err(KeyError(f"unknown source {name!r}"), 404)
        referencing = [t.name for t in runtime.catalog.triggers if name in t.sources]
        gone: list[str] = []
        if referencing and not cascade:
            _err(ValueError(f"source {name!r} is used by triggers {referencing}; "
                            f"delete them too (cascade) or remove it from those triggers first"),
                 409)
        if cascade:
            gone = _delete_dependents("source", name)
        store.delete_catalog_source(name)
        purged = store.purge_events(name) if purge_events else 0
        runtime.reload_catalog()
        return {"ok": True, "purged_events": purged, "deleted": gone}

    @app.post("/api/sources/{name}/pause")
    async def pause_source(name: str):
        if name not in runtime.catalog.sources:
            _err(KeyError(f"unknown source {name!r}"), 404)
        store.set_source_paused(name, True)
        runtime.reload_catalog()
        return {"ok": True}

    @app.post("/api/sources/{name}/resume")
    async def resume_source(name: str):
        if name not in runtime.catalog.sources:
            _err(KeyError(f"unknown source {name!r}"), 404)
        store.set_source_paused(name, False)
        runtime.reload_catalog()
        return {"ok": True}

    @app.post("/api/sources/test")
    async def test_source(body: SourceIn):
        try:
            validate_source_dict(body.model_dump())
            d = body.model_dump()
            d["config"] = normalize_config(body.connector, body.config)
        except CatalogError as e:
            _err(e)
        return await runtime.test_source(_source_from_dict(d))

    @app.get("/api/sources/{name}/events")
    async def source_events(name: str, limit: int = 50):
        if name not in {s["name"] for s in store.list_catalog_sources()}:
            _err(KeyError(f"unknown source {name!r}"), 404)
        return store.recent_events(source=name, limit=min(limit, 500))

    @app.get("/api/sources/{name}/fields")
    async def source_fields(name: str, limit: int = 500):
        """What this source actually contains: the connector's normalized fields (the things you can
        key/label on), each with how many sampled events carry it and its top values. Makes the
        otherwise-invisible normalized structure visible, so a key is chosen, not guessed."""
        from collections import Counter

        from .connectors import build_connector
        cfg = runtime.catalog.sources.get(name)
        if cfg is None:
            _err(KeyError(f"unknown source {name!r}"), 404)
        conn = build_connector(cfg, store)

        # Which fields are actual entity axes (the things you read/alert by). Match a declared label
        # by the field's last path segment, so a nested context (e.g. a Prometheus label set stored
        # under `metric`) still maps: `metric.service` counts as the declared `service` label.
        label_specs = (cfg.config.get("labels") or []) if isinstance(cfg.config, dict) else []
        label_names = {s.get("field") or s.get("name") for s in label_specs} | {s.get("name") for s in label_specs}
        label_names.discard(None)
        key_names = {(s.get("field") or s.get("name")) for s in label_specs if s.get("primary")}
        key_names.discard(None)

        from .config import extract_labels

        counts: dict[str, Counter] = {}
        label_counts: dict[str, Counter] = {}   # the EXTRACTED value of each declared label
        sampled = 0
        for payload in store.recent_payloads(name, min(limit, 2000)):
            ctx = conn.label_context(payload)
            if not isinstance(ctx, dict):
                continue
            sampled += 1
            for k, v in ctx.items():
                if isinstance(v, dict):                 # explode one level: metric.service, metric.endpoint, …
                    for sk, sv in v.items():
                        if sv not in (None, ""):
                            counts.setdefault(f"{k}.{sk}", Counter())[str(sv)] += 1
                elif v not in (None, ""):
                    counts.setdefault(k, Counter())[str(v)] += 1
            # Profile the actual EXTRACTED labels too (the same extraction ingest uses), so a
            # derived label (regex/const/map over a raw field) is visible with its real coverage
            # and values — not just the raw field it reads from.
            for lname, lval in extract_labels(label_specs, ctx).items():
                if lval not in (None, ""):
                    label_counts.setdefault(lname, Counter())[str(lval)] += 1

        def entry(fname, help_="", primary_default=False):
            vals = counts.get(fname, Counter())
            seg = fname.rsplit(".", 1)[-1]
            return {"name": fname, "help": help_, "primary_default": primary_default,
                    "coverage": sum(vals.values()), "distinct": len(vals),
                    "is_label": seg in label_names or fname in label_names,
                    "is_key": seg in key_names or fname in key_names,
                    "values": [{"value": val, "events": c} for val, c in vals.most_common(8)]}

        fields, seen = [], set()
        for spec in getattr(type(conn), "PROVIDES", None) or []:   # advertised fields, in order
            fields.append(entry(spec["name"], spec.get("help", ""), spec.get("primary", False)))
            seen.add(spec["name"])
        for fname in counts:                                       # plus observed (flattened) fields
            if fname not in seen:
                fields.append(entry(fname))
        # entity axes first (key, then label), then the rest — lead with what you'd actually key on.
        fields.sort(key=lambda f: (0 if f["is_key"] else 1 if f["is_label"] else 2))

        # Declared labels, profiled by their EXTRACTED value — the curated axes you read/alert by,
        # surfaced whether they map to a raw field (service) or are derived (http_status). Coverage
        # of 0 means the extraction matched nothing (e.g. a bad regex) — useful on its own.
        labels = []
        for s in label_specs:
            lname = s.get("name")
            if not lname:
                continue
            vals = label_counts.get(lname, Counter())
            labels.append({"name": lname, "is_key": bool(s.get("primary")),
                           "coverage": sum(vals.values()), "distinct": len(vals),
                           "values": [{"value": val, "events": c} for val, c in vals.most_common(8)]})
        labels.sort(key=lambda l: 0 if l["is_key"] else 1)

        return {"sampled": sampled, "fields": fields, "labels": labels}

    # ── in-app agent (the Ask view) — server-side chat loop over the read API ──
    def _record_ask_usage(model: str, usage: dict, key_source: str = "", kind: str = "anthropic",
                          provider_id: str | None = None) -> None:
        """One ledger row per Ask turn (console chat or Slack /ask), so the cell's spend meter
        covers everything that talks to Anthropic, not just agent runs. `key_source` is the
        origin of the key that PAID for this turn, captured by the caller when it resolved the
        key — never re-resolved here, the stored key could have changed mid-turn."""
        from .pricing import price_usage
        store.record_model_usage(
            "ask", "", "", model, usage["calls"],
            usage["input_tokens"], usage["output_tokens"],
            usage["cache_creation_input_tokens"], usage["cache_read_input_tokens"],
            price_usage(kind, model, usage),
            key_source=key_source or None, provider=provider_id)

    @app.post("/api/agent/chat")
    async def agent_chat(request: Request):
        from .agent import BUILD_STEPS, run_agent, traced_name
        # ONE key for the whole instance: env, else the console-stored one. There used to be an
        # `X-Anthropic-Key` header override, which the console filled from localStorage — so a key
        # added on the Ask page made Ask work while Slack and trigger-woken agents still reported
        # none configured, having no browser to read it from (NF-125).
        provider, key_origin = resolve_provider(store)
        if provider is None:
            _err(ValueError("add a model provider under Settings to use the assistant"), 400)
        default_pid = providers_mod.default_id(store)
        body = await request.json()
        # the daemon's own token, so the agent's tool self-calls clear the auth middleware
        self_headers = {"Authorization": f"Bearer {AUTH_TOKEN}"} if AUTH_TOKEN else {}
        # mode "build" + step (sources|watch|agent) is the AI-guided project builder:
        # same loop, same endpoint, a step-scoped toolset (tares/agent.py, TR-242)
        mode = "build" if body.get("mode") == "build" else "ask"
        step = str(body.get("step") or "") or None
        if mode == "build" and step not in BUILD_STEPS:
            _err(ValueError(f"build step must be one of {', '.join(BUILD_STEPS)}"), 400)
        # the console's id for one conversation (one project build), the turns' session.id
        session = body.get("session")
        session = (session.strip()[:128] or None) if isinstance(session, str) else None
        return StreamingResponse(
            run_agent(provider, body.get("messages") or [],
                      # the default provider's default model, not a Claude id on a router
                      model=body.get("model") or providers_mod.default_model_for(store, default_pid),
                      self_headers=self_headers,
                      on_usage=lambda m, u: _record_ask_usage(m, u, key_source=key_origin,
                                                              kind=provider.kind,
                                                              provider_id=default_pid),
                      tracer=tracing.tracer_for(traced_name(mode)), mode=mode, step=step,
                      session=session),
            media_type="text/event-stream")

    # ── MCP connections — external tool servers a Tares agent can opt into ─────
    # The registry: URL + optional auth header. The auth value is a secret: never returned,
    # blank-to-keep on update, exported only with secrets included.
    def _mcp_row(m: dict) -> dict:
        from .github_credentials import credential_name, is_credential_ref
        ref = m["auth_value"] if is_credential_ref(m["auth_value"]) else ""
        return {"name": m["name"], "url": m["url"], "auth_header": m["auth_header"],
                "auth_value_configured": bool(m["auth_value"]),
                # a credential reference is not a secret: the console can show which one is used
                "auth_credential": credential_name(ref) if ref else "",
                "headers": m.get("headers") or {}, "updated_at": m["updated_at"],
                "project": m.get("owned_by"),
                "owned_by": m.get("owned_by"), "customized": bool(m.get("customized"))}

    def _clean_headers(headers: dict | None) -> dict:
        out = {}
        for k, v in (headers or {}).items():
            k = str(k).strip()
            if not k:
                continue
            if not re.fullmatch(r"[A-Za-z0-9-]+", k):
                _err(ValueError(f"header name {k!r} must be a header name"))
            out[k] = str(v).strip()
        return out

    @app.get("/api/resources")
    async def list_all_resources():
        """Every part on the cell (sources, wake-ups, agents, tools, skills, project keys), each
        with the projects that use it: the All resources page."""
        from . import resources as resources_mod
        return await asyncio.to_thread(resources_mod.list_resources, store, runtime.catalog,
                                       runtime.health_snapshot())

    @app.get("/api/mcp-servers")
    async def list_mcp_servers():
        return {"servers": [_mcp_row(m) for m in store.list_mcp_servers()]}

    @app.post("/api/mcp-servers", status_code=201)
    async def create_mcp_server(body: McpServerIn):
        if store.get_mcp_server(body.name) is not None:
            _err(ValueError(f"mcp server {body.name!r} already exists"), 409)
        uid = _resolve_project(body.project)
        try:
            validate_mcp_server_dict(body.model_dump())
        except CatalogError as e:
            _err(e)
        store.upsert_mcp_server(body.name, body.url.strip(), body.auth_header.strip(),
                                _check_credential_ref(body.auth_value), _clean_headers(body.headers))
        store.put_in_project("mcp_server", body.name, uid)
        return {"ok": True, "project": uid}

    def _check_credential_ref(value: str) -> str:
        """`credential:github/<name>` must name a stored credential; anything else passes through
        as the literal header value."""
        from .github_credentials import credential_name, is_credential_ref
        if is_credential_ref(value) and store.get_github_credential(credential_name(value)) is None:
            _err(ValueError(f"GitHub credential {credential_name(value)!r} not found "
                            "(Settings > GitHub)"), 404)
        return value

    @app.put("/api/mcp-servers/{name}")
    async def update_mcp_server(name: str, body: McpServerIn):
        existing = store.get_mcp_server(name)
        if existing is None:
            _err(KeyError(f"unknown mcp server {name!r}"), 404)
        if body.name != name:
            _err(ValueError("renaming a server is not supported; delete and recreate"), 400)
        try:
            validate_mcp_server_dict(body.model_dump())
        except CatalogError as e:
            _err(e)
        value = body.auth_value or existing.get("auth_value", "")   # blank-to-keep
        # a project named joins it as a user; the server stays in its other projects (P-TR-216)
        uid = _resolve_project(body.project, default=False)
        store.upsert_mcp_server(name, body.url.strip(), body.auth_header.strip(),
                                _check_credential_ref(value), _clean_headers(body.headers))
        store.mark_customized("mcp_server", name)
        if uid:
            store.put_in_project("mcp_server", name, uid)
        return {"ok": True, "project": uid or existing.get("owned_by")}

    @app.delete("/api/mcp-servers/{name}")
    async def delete_mcp_server(name: str):
        if store.get_mcp_server(name) is None:
            _err(KeyError(f"unknown mcp server {name!r}"), 404)
        store.delete_mcp_server(name)
        return {"ok": True}

    @app.post("/api/mcp-servers/{name}/test")
    async def test_mcp_server(name: str):
        """Connect with the stored config and list the server's tools — the proof a connection
        works, and the tool list the agent form will offer."""
        server = store.get_mcp_server(name)
        if server is None:
            _err(KeyError(f"unknown mcp server {name!r}"), 404)
        from .mcp_client import list_remote_tools, resolve_servers
        try:
            tools = await asyncio.wait_for(
                list_remote_tools((await resolve_servers(store, [server]))[0]), timeout=20)
        except Exception as e:
            detail = f"{type(e).__name__}: {str(e)[:200]}" if str(e).strip() else type(e).__name__
            _record_tool_test(name, False, 0, detail)
            return {"ok": False, "error": detail, "tools": []}
        _record_tool_test(name, True, len(tools or []), None)
        return {"ok": True, "tools": tools}

    # ── GitHub credentials: a token stored once, referenced by sources and MCP servers ──
    # Same write-only contract as the other credentials: the token is never returned, blank-to-keep
    # on update, and the console can list, test and delete.
    from .github_credentials import (APP_KINDS, forget_repos, get_token, list_repos_for, list_tree,
                                     redact as _gh_redact, test_credential)
    from . import github_app as _gh_app

    def _gh_users(name: str) -> dict:
        """Where a credential is used, so deleting one is an informed act."""
        sources = [s["name"] for s in store.list_catalog_sources()
                   if (s.get("config") or {}).get("credential") == name]
        ref = f"credential:github/{name}"
        servers = [m["name"] for m in store.list_mcp_servers() if m.get("auth_value") == ref]
        return {"sources": sources, "mcp_servers": servers}

    def _gh_app_source(name: str) -> str | None:
        """The webhook source an App credential feeds, if it exists."""
        return next((s["name"] for s in store.list_catalog_sources()
                     if s["connector"] == "github_app"
                     and (s.get("config") or {}).get("credential") == name), None)

    def _gh_deliveries(cred: dict) -> dict:
        """What the settings row shows about an App's webhook: last delivery, refusals, drops."""
        src = _gh_app_source(cred["name"])
        if not src:
            return {}
        from .connectors.github_app import STATS
        st = STATS.get(src) or {}
        cfg = runtime.catalog.sources.get(src)
        return {"source": src, "ingest_key": cfg.ingest_key if cfg else None,
                "deliveries": {"received": st.get("deliveries", 0), "stored": st.get("stored", 0),
                               "last_at": st.get("last_at"), "last_event": st.get("last_event"),
                               "unknown_installation": st.get("unknown_installation", 0),
                               "rejected_signature": runtime.rejected_deliveries.get(src, 0)}}

    def _gh_row(c: dict) -> dict:
        return {**_gh_redact(c), **_gh_users(c["name"]),
                **(_gh_deliveries(c) if c.get("kind") in APP_KINDS else {})}

    def _ensure_gh_app_source(name: str, ingest_key: str | None = None) -> str:
        """The GitHub App source a credential feeds, created when missing: `github` when that name
        is free, else `github_<credential>`. One per credential; it is a part of the cell like any
        source (Default project until a project uses it)."""
        existing = _gh_app_source(name)
        if existing:
            if ingest_key and runtime.catalog.sources[existing].ingest_key != ingest_key:
                # an App recreated under the same name: its hook URL carries the new key
                store.set_source_ingest_key(existing, ingest_key)
                runtime.reload_catalog()
            return existing
        src = "github" if "github" not in runtime.catalog.sources else \
            "github_" + re.sub(r"[^A-Za-z0-9_]", "_", name)
        store.upsert_catalog_source(src, source_type_for("github_app"), "github_app", "1m",
                                    normalize_config("github_app", {"credential": name}),
                                    ingest_key=ingest_key)
        store.put_in_project("source", src, store.default_project_id(), creator=True)
        runtime.reload_catalog()
        return src

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
    async def list_github_credentials():
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
            src = _ensure_gh_app_source(name)
            return {"ok": True, "account": "", "source": src,
                    "ingest_key": runtime.catalog.sources[src].ingest_key}
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
            src = _ensure_gh_app_source(name)
            return {"ok": True, "account": body.account.strip(), "source": src,
                    "ingest_key": runtime.catalog.sources[src].ingest_key}
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
    async def delete_github_credential(name: str):
        if store.get_github_credential(name) is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        store.delete_github_credential(name)
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

    @app.post("/api/integrations/github/{name}/mcp")
    async def github_credential_mcp(name: str, body: GithubMcpIn):
        """The GitHub MCP server for this credential (GitHub's hosted server, authenticated with
        the credential, so an App credential's agents act as the App): `github-<name>` with write
        toolsets, `githubro-<name>` read-only. Created once, a shared part any project uses;
        the agent form adds the returned name to the agent's MCP servers."""
        cred = store.get_github_credential(name)
        if cred is None:
            _err(KeyError(f"unknown GitHub credential {name!r}"), 404)
        if cred.get("api_url"):
            # GitHub's hosted MCP server is github.com's: a GitHub Enterprise token must not go there
            _err(ValueError("GitHub's MCP server serves github.com only; this credential is for "
                            "GitHub Enterprise"), 400)
        from .github_credentials import CREDENTIAL_PREFIX
        # two prefixes, not a suffix: credential names allow "-", so "x-read" would collide
        server = ("github-" if body.write else "githubro-") + name
        if store.get_mcp_server(server) is None:
            headers = {"X-MCP-Toolsets": "repos,pull_requests,issues"}
            if not body.write:
                headers["X-MCP-Readonly"] = "true"
            store.upsert_mcp_server(server, GITHUB_MCP_URL, "Authorization",
                                    CREDENTIAL_PREFIX + name, headers)
            store.put_in_project("mcp_server", server, store.default_project_id(), creator=True)
        return {"ok": True, "server": server}

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
    #    the App's id, key and webhook secret, the credential and its webhook source are stored.
    # 3. The person installs it; GitHub sends the browser to .../apps/installed?installation_id=...
    # The two callbacks are public to the auth middleware (a redirect has no Tares key) and gated
    # by the signed state, or for the install by proving the installation belongs to our App.

    def _gh_settings(params: dict) -> RedirectResponse:
        from urllib.parse import urlencode
        return RedirectResponse(f"/settings?{urlencode({'tab': 'github', **params})}",
                                status_code=303)

    @app.post("/api/integrations/github/apps")
    async def create_github_app(body: GithubAppCreateIn, request: Request):
        from .setup_flow import public_base
        name = _check_gh_name(body.name)
        base = (body.public_url.strip() or public_base(str(request.base_url))).rstrip("/")
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
        _ensure_gh_app_source(name, data["key"])
        return _gh_settings({"github": name, "event": "created"})

    @app.get("/api/integrations/github/{name}/install")
    async def github_app_install_link(name: str):
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

    # ── Ask sessions — server-side chat history, so a conversation survives navigation and a
    # console reopened tomorrow can pick up where it left off. The console PUTs the whole session
    # after each exchange; the store keeps the newest 50.
    @app.get("/api/ask/sessions")
    async def ask_sessions():
        return {"sessions": store.list_ask_sessions()}

    @app.get("/api/ask/sessions/{sid}")
    async def ask_session(sid: str):
        row = store.get_ask_session(sid)
        if row is None:
            _err(KeyError(f"unknown session {sid!r}"), 404)
        return row

    @app.put("/api/ask/sessions/{sid}")
    async def save_ask_session(sid: str, body: AskSessionIn):
        if not re.fullmatch(r"[0-9a-f]{16,64}", sid):
            _err(ValueError("session id must be a hex id"), 400)
        # a runaway transcript should degrade the one session, not the instance
        if len(body.state) > 2_000_000:
            _err(ValueError("session too large to save (2 MB cap)"), 413)
        store.upsert_ask_session(sid, body.title[:120], body.state)
        return {"ok": True}

    @app.delete("/api/ask/sessions/{sid}")
    async def remove_ask_session(sid: str):
        if not store.delete_ask_session(sid):
            _err(KeyError(f"unknown session {sid!r}"), 404)
        return {"ok": True}

    @app.get("/api/mcp/tools")
    async def mcp_tool_list():
        """The tools an external agent gets over MCP — read straight from the MCP server's own
        registration (so it reflects read-only gating). Powers the Connect view."""
        from .mcp_server import mcp as mcp_srv
        return [{"name": t.name, "description": (t.description or "").strip()}
                for t in await mcp_srv.list_tools()]

    # ── management API: triggers ──────────────────────────────────────────────
    # A trigger watches `sources` (narrowed by `filters`) and belongs to exactly one project; the
    # sources it reads are members of that project (a trigger call adds the ones that are not).
    def _trigger_row(t: dict) -> dict:
        return {"name": t["name"], "project": t.get("owned_by"), "sources": t["sources"],
                "filters": t["filters"], "key_field": t["key_field"],
                "description": t.get("description") or "",
                "condition": t["condition"], "emit": t["emit"], "cooldown": t["cooldown"],
                "paused": t["paused"], "owned_by": t.get("owned_by"),
                "customized": t["customized"]}

    @app.get("/api/triggers")
    async def list_triggers():
        return [_trigger_row(t) for t in store.list_catalog_triggers()]

    def _check_trigger(body: TriggerIn, name: str) -> dict:
        if body.view not in (None, ""):
            _err(ValueError(VIEWS_REMOVED), 400)
        raw = {**body.model_dump(exclude={"view", "project"}), "name": name}
        try:
            validate_trigger_dict(raw, set(runtime.catalog.sources))
        except CatalogError as e:
            _err(e)
        return raw

    def _trigger_description(body: TriggerIn) -> str | None:
        """The description to store: None keeps the current one (a client that does not send
        it), "" clears it. Already validated by _check_trigger."""
        if body.description is None:
            return None
        return normalize_trigger_description(body.description)

    def _place_trigger(name: str, uid: str, sources: list[str]) -> None:
        """Put the trigger in `uid`, with its sources as members. It stays in the other projects
        that use it (P-TR-216); one that was in the default project only for want of another
        moves, with the default project's wiring of it."""
        store.put_in_project("trigger", name, uid)
        for src in sources:
            store.put_in_project("source", src, uid)

    @app.post("/api/triggers", status_code=201)
    async def create_trigger(body: TriggerIn):
        if body.name in {t.name for t in runtime.catalog.triggers}:
            _err(ValueError(f"trigger {body.name!r} already exists"), 409)
        raw = _check_trigger(body, body.name)
        uid = _resolve_project(body.project)
        store.upsert_catalog_trigger(body.name, raw["sources"], body.condition, body.emit,
                                     body.cooldown, filters=raw.get("filters") or [],
                                     key_field=raw.get("key_field") or "",
                                     description=_trigger_description(body))
        _place_trigger(body.name, uid, raw["sources"])
        runtime.reload_catalog()
        return {"ok": True, "project": uid}

    @app.put("/api/triggers/{name}")
    async def update_trigger(name: str, body: TriggerIn):
        current = next((t for t in store.list_catalog_triggers() if t["name"] == name), None)
        if current is None:
            _err(KeyError(f"unknown trigger {name!r}"), 404)
        if body.name != name:
            _err(ValueError("renaming a trigger is not supported; delete and recreate"), 400)
        raw = _check_trigger(body, name)
        uid = _resolve_project(body.project, default=False) or current.get("owned_by") \
            or store.default_project_id()
        store.upsert_catalog_trigger(name, raw["sources"], body.condition, body.emit,
                                     body.cooldown, filters=raw.get("filters") or [],
                                     key_field=raw.get("key_field") or "",
                                     description=_trigger_description(body))
        store.mark_customized("trigger", name)
        _place_trigger(name, uid, raw["sources"])
        runtime.reload_catalog()
        return {"ok": True, "project": uid}

    @app.delete("/api/triggers/{name}")
    async def delete_trigger(name: str):
        if name not in {t.name for t in runtime.catalog.triggers}:
            _err(KeyError(f"unknown trigger {name!r}"), 404)
        # it leaves every project, and the agents it woke stay, without a trigger of their own
        cleared = store.delete_catalog_trigger(name)
        store.remove_subscriptions_by_trigger(name)
        runtime.reload_catalog()
        return {"ok": True, "agents_without_trigger": cleared}

    @app.post("/api/triggers/{name}/pause")
    async def pause_trigger(name: str):
        if name not in {t.name for t in runtime.catalog.triggers}:
            _err(KeyError(f"unknown trigger {name!r}"), 404)
        store.set_trigger_paused(name, True)
        runtime.reload_catalog()
        return {"ok": True}

    @app.post("/api/triggers/{name}/resume")
    async def resume_trigger(name: str):
        if name not in {t.name for t in runtime.catalog.triggers}:
            _err(KeyError(f"unknown trigger {name!r}"), 404)
        store.set_trigger_paused(name, False)
        runtime.reload_catalog()
        return {"ok": True}

    # ── management API: Tares agents (a prompt attached to a trigger) ──────
    # Definitions live under /api/agents/builtin; the roster of everything a trigger wakes (these
    # PLUS external subscribers) is /api/agents. A Tares agent is "enabled" exactly when it has a
    # subscription to its trigger — the same wiring an external agent has.
    def _agent_enabled(name: str, project: str | None = None) -> bool:
        """On: some wake-up wakes it (in `project`, or in any project)."""
        return store.agent_enabled(name, project)

    def _agent_payload(body: AgentIn, uid: str, name: str = "") -> dict:
        """Validate against the live catalog, including the loop guard (an agent may not be woken by
        the findings source it writes into). Any trigger, MCP server and handoff target on the
        cell will do (P-TR-216: parts are shared); the project's wiring says how it uses them."""
        triggers = {t["name"]: t for t in store.list_catalog_triggers()}
        servers = {m["name"]: m for m in store.list_mcp_servers()}
        raw = {**body.model_dump(exclude={"project"}), **({"name": name} if name else {})}
        try:
            validate_agent_dict(raw, set(triggers), triggers, set(servers))
            raw["verdicts"] = normalize_verdicts(raw["name"], body.verdicts)
        except CatalogError as e:
            _err(e)
        if body.handoffs is not None:
            # the targets: agents that exist (TR-334)
            try:
                raw["handoffs"] = normalize_handoffs(raw["name"], body.handoffs)
                check_handoff_targets(raw["name"], raw["handoffs"],
                                      {a["name"]: a.get("owned_by")
                                       for a in store.list_catalog_agents()
                                       if a["name"] != raw["name"]}, uid)
            except CatalogError as e:
                _err(e)
        return raw

    @app.get("/api/agents/builtin")
    async def list_builtin_agents(project: str | None = None):
        """Tares agent definitions plus the state the UI needs to explain why one isn't running:
        no key configured is the common case on a fresh install and looks identical to "disabled"
        without this."""
        provider, origin = resolve_provider(store)
        in_project = _resolve_project(project, default=False) if project else None
        # with a project: its runs only (P-TR-216: an agent's runs belong to the projects whose
        # wiring started them)
        stats = store.agent_stats(in_project)
        zero = {"runs": 0, "ok": 0, "finished": 0, "avg_duration_ms": None,
                "cost_usd": None, "input_tokens": 0, "output_tokens": 0, "uncosted_runs": 0}
        rows = []
        # with ?project=: each agent's trigger, handoffs and on/off are that project's wiring
        mine = ("id IN (SELECT run_id FROM run_projects WHERE project = ?)", [in_project]) \
            if in_project else ("", None)
        for a in store.list_catalog_agents(in_project):
            runs = store.list_agent_runs(a["name"], limit=1, where_sql=mine[0],
                                         where_params=mine[1])
            rows.append({"name": a["name"], "trigger": a["trigger"], "prompt": a["prompt"],
                         "stats": stats.get(a["name"]) or zero,
                         "slack_configured": bool(a.get("slack_webhook")),
                         "model": a.get("model") or "",
                         "provider": a.get("provider") or "",
                         # what the next run resolves to, so the page can say "ran on the
                         # default" when the named provider is gone
                         "effective_provider": providers_mod.resolve_for_agent(store, a)[2],
                         "slack_channel": a.get("slack_channel") or "",
                         "webhook_url": a.get("webhook_url") or "",
                         "webhook_token_configured": bool(a.get("webhook_token")),
                         "webhook_key_label": a.get("webhook_key_label") or "",
                         "mcp_servers": a.get("mcp_servers") or [],
                         "max_rounds": a.get("max_rounds"),
                         "budget_usd": a.get("budget_usd"),
                         "daily_cap": a.get("daily_cap"),
                         "handoffs": a.get("handoffs") or [],
                         "concludes": bool(a.get("concludes")),
                         "verdicts": a.get("verdicts") or [],
                         "github": a.get("github") or "",
                         # it gets the conclude tool (set to, or its prompt names it): its runs
                         # end with an outcome and a verdict worth a column of their own
                         "offers_conclude": offers_conclude(a),
                         "effective_max_rounds": effective_max_rounds(a),
                         "enabled": a["enabled"], "updated_at": a.get("updated_at"),
                         "project": in_project or a.get("owned_by"),
                         "projects": store.projects_using("agent", a["name"]),
                         "owned_by": a.get("owned_by"), "customized": bool(a.get("customized")),
                         "last_run": runs[0] if runs else None})
        plist = providers_mod.list_providers(store)
        return {"agents": rows, "key_configured": provider is not None, "key_source": origin,
                "models": AGENT_MODELS, "default_model": AGENT_DEFAULT_MODEL,
                "providers": plist["providers"], "default_provider": plist["default"],
                "default_models": {p["id"]: providers_mod.default_model_for(store, p["id"])
                                   for p in plist["providers"]},
                "default_max_rounds": AGENT_MAX_ROUNDS,
                "default_max_rounds_with_mcp": AGENT_MAX_ROUNDS_WITH_MCP,
                "max_rounds_limit": AGENT_MAX_ROUNDS_LIMIT,
                "slack_workspace": bool(resolve_slack_token(store)[0]),
                "presets": [{"id": k, **v} for k, v in AGENT_PRESETS.items()]}

    def _check_agent_github(value: str | None, current: str = "") -> str | None:
        """An agent's `github` must name a stored GitHub credential (None and "" pass through;
        the value it already has passes too, so a deleted credential never blocks a save)."""
        if value and value.strip() != current and store.get_github_credential(value.strip()) is None:
            _err(ValueError(f"GitHub credential {value!r} not found (Settings > GitHub)"), 404)
        return value.strip() if value else value

    @app.post("/api/agents/builtin", status_code=201)
    async def create_builtin_agent(body: AgentIn):
        if not body.name:
            _err(ValueError("name is required"), 400)
        if store.get_catalog_agent(body.name) is not None:
            _err(ValueError(f"agent {body.name!r} already exists"), 409)
        uid = _resolve_project(body.project)
        raw = _agent_payload(body, uid)
        store.upsert_catalog_agent(body.name, body.trigger, body.prompt, body.slack_webhook,
                                   body.model, body.slack_channel,
                                   body.webhook_url, body.webhook_token, body.mcp_servers,
                                   body.max_rounds, body.budget_usd,
                                   webhook_key_label=body.webhook_key_label,
                                   provider=body.provider.strip(),
                                   handoffs=raw.get("handoffs") or [],
                                   concludes=bool(body.concludes),
                                   verdicts=raw.get("verdicts") or [],
                                   github=_check_agent_github(body.github) or "")
        store.put_in_project("agent", body.name, uid, creator=True)
        runtime.reload_catalog()
        return {"ok": True, "enabled": False, "project": uid,
                "note": "agents start disabled; enable it to run on the next firing"}

    @app.put("/api/agents/builtin/{name}")
    async def update_builtin_agent(name: str, body: AgentIn):
        existing = store.get_catalog_agent(name)
        if existing is None:
            _err(KeyError(f"unknown agent {name!r}"), 404)
        # the path names the agent; a body name is optional and only checked for a rename attempt
        if body.name and body.name != name:
            _err(ValueError("renaming an agent is not supported; delete and recreate"), 400)
        uid = _resolve_project(body.project, default=False) or existing.get("owned_by") \
            or store.default_project_id()
        raw = _agent_payload(body, uid, name)
        # blank-to-keep for the webhook, matching the connector-secret convention: the UI never
        # receives the stored URL back, so an unedited form must not wipe it.
        hook = "" if body.slack_webhook_clear else (body.slack_webhook or existing.get("slack_webhook", ""))
        # blank-to-keep for the write-back token too (a secret the UI never gets back). Clearing
        # it means clearing the URL: a webhook without its token is a delivery that 401s forever.
        wtoken = body.webhook_token or (existing.get("webhook_token", "") if body.webhook_url else "")
        # model, channel and webhook URL are not secrets: the form always shows the stored value,
        # so what the body says is what the user wants — including blank (removed).
        # The trigger and handoffs are the project's wiring (P-TR-216): edited from another
        # project than its maker, the agent's own fields stay and that project's wiring changes.
        maker = existing.get("owned_by")
        here = uid == maker or not maker
        store.upsert_catalog_agent(name, body.trigger if here else store.get_catalog_agent(name)["trigger"],
                                   body.prompt, hook,
                                   body.model, body.slack_channel,
                                   body.webhook_url, wtoken, body.mcp_servers, body.max_rounds,
                                   body.budget_usd, webhook_key_label=body.webhook_key_label,
                                   provider=body.provider.strip(),
                                   # the form has no daily cap field; keep what a project set
                                   daily_cap=existing.get("daily_cap"),
                                   # absent (None) keeps the stored handoffs
                                   handoffs=raw.get("handoffs") if here else None,
                                   concludes=body.concludes,
                                   verdicts=(raw.get("verdicts") if body.verdicts is not None
                                             else None),
                                   github=_check_agent_github(body.github,
                                                              existing.get("github") or ""))
        store.mark_customized("agent", name)
        store.put_in_project("agent", name, uid)
        if not here:
            store.wire_agent(name, uid, body.trigger, raw.get("handoffs"))
        runtime.reload_catalog()
        return {"ok": True}

    @app.delete("/api/agents/builtin/{name}")
    async def delete_builtin_agent(name: str):
        if store.get_catalog_agent(name) is None:
            _err(KeyError(f"unknown agent {name!r}"), 404)
        # the agents that handed off to it lose that handoff, in every project (the store drops
        # its wiring with the agent)
        handed = sorted({h["from_agent"] for h in store.list_handoffs() if h["agent"] == name})
        store.delete_catalog_agent(name)
        runtime.reload_catalog()
        return {"ok": True, "handoffs_removed_from": handed}

    def _wired_project(name: str, project: str | None) -> str:
        """The project an agent is turned on or off in: the one named, else the one that made
        it. The agent joins it, wired with its own trigger, when it is not wired there yet."""
        agent = store.get_catalog_agent(name)
        uid = _resolve_project(project, default=False) if project else None
        uid = uid or agent.get("owned_by") or store.default_project_id()
        if not store.list_wakes(project=uid, agent=name):
            if not agent.get("trigger"):
                _err(ValueError(f"{name} has no trigger of its own, so there is nothing to turn "
                                "on: only a handoff starts it. Pick a trigger under Edit to "
                                "wake it on its own."), 409)
            store.put_in_project("agent", name, uid)
            store.wire_agent(name, uid, agent["trigger"], None)
        return uid

    @app.post("/api/agents/builtin/{name}/enable")
    async def enable_builtin_agent(name: str, project: str | None = None):
        """On in a project (the one named, else the one that made it): its wake-ups there wake
        it. Idempotent."""
        agent = store.get_catalog_agent(name)
        if agent is None:
            _err(KeyError(f"unknown agent {name!r}"), 404)
        if providers_mod.resolve_for_agent(store, agent)[0] is None:
            _err(ValueError("no model provider configured; add one under Settings, or set "
                            "ANTHROPIC_API_KEY, before enabling an agent"))
        uid = _wired_project(name, project)
        store.set_agent_enabled(name, True, project=uid)
        runtime.reload_catalog()
        return {"ok": True, "enabled": True, "project": uid}

    @app.post("/api/agents/builtin/{name}/disable")
    async def disable_builtin_agent(name: str, project: str | None = None):
        """Off in a project (the one named), or everywhere when none is named."""
        if store.get_catalog_agent(name) is None:
            _err(KeyError(f"unknown agent {name!r}"), 404)
        uid = _resolve_project(project, default=False) if project else None
        store.set_agent_enabled(name, False, project=uid)
        runtime.reload_catalog()
        return {"ok": True, "enabled": False, **({"project": uid} if uid else {})}

    @app.post("/api/agents/builtin/{name}/runs/{run_id}/rerun", status_code=201)
    async def rerun_builtin_agent(name: str, run_id: str):
        """Run the agent again with the same inputs as an earlier run: same trigger, same key,
        and the firing's payload when a firing woke it (a manual or bootstrap run reruns with an
        empty note; the timeline the agent reads is the real input either way). Same run record,
        caps and dedupe as any run."""
        if store.get_catalog_agent(name) is None:
            _err(KeyError(f"unknown agent {name!r}"), 404)
        run = store.get_agent_run(run_id)
        if run is None or run["agent"] != name:
            _err(KeyError(f"unknown run {run_id!r} for agent {name!r}"), 404)
        if run["status"] == "running":
            _err(ValueError("that run is still going; wait for it to finish"))
        payload = ""
        if run.get("dispatch_id"):
            d = store.get_dispatch(run["dispatch_id"])
            payload = (d or {}).get("payload") or ""
        rid = dispatcher.agents.run_now(name, run["trigger"], run["key"], payload,
                                        woken_by="rerun", parent_run_id=run_id,
                                        # a rerun belongs where the run it repeats did
                                        project=run.get("project"))
        if rid is None:
            _err(ValueError(f"the agent is already running for {run['key']!r}"), 409)
        return {"ok": True, "run_id": rid, "rerun_of": run_id}

    _RUN_STATUSES = {"running", "ok", "empty", "failed", "capped", "exhausted"}

    @app.get("/api/agents/builtin/{name}/runs")
    async def builtin_agent_runs(name: str, limit: int = 50, offset: int = 0, status: str = "",
                                 project: str | None = None):
        """The operational record — status, duration, errors. Distinct from findings, which are
        events on the entity's timeline; a failed run must never look like a conclusion.
        `status` narrows to one run status; `offset` pages (a capped agent's list is mostly
        capped rows, and the ok runs are what a reader looks for, TR-267)."""
        if store.get_catalog_agent(name) is None:
            _err(KeyError(f"unknown agent {name!r}"), 404)
        if status and status not in _RUN_STATUSES:
            _err(ValueError(f"status must be one of {', '.join(sorted(_RUN_STATUSES))}"), 400)
        # with a project: the runs that belong to it (P-TR-216)
        uid = _resolve_project(project, default=False) if project else None
        return store.list_agent_runs(name, limit=min(max(1, limit), 200), offset=offset,
                                     status=status or None,
                                     where_sql="id IN (SELECT run_id FROM run_projects WHERE "
                                               "project = ?)" if uid else "",
                                     where_params=[uid] if uid else None)

    # ── the Anthropic key: a stored key wins, env is the fallback ────────────
    @app.get("/api/settings/anthropic-key")
    async def get_anthropic_key():
        """Never returns the key — only whether one is resolvable and where it came from. A key
        saved here takes over from the deployment's env key; `env_overrides` is kept for API
        compatibility and can now only be true when no key is stored."""
        headers, origin = resolve_anthropic_headers(store)
        return {"configured": bool(headers), "source": origin,
                "stored": bool(store.get_setting("anthropic_key")),
                "env_overrides": origin.startswith("env:")}

    @app.put("/api/settings/anthropic-key")
    async def set_anthropic_key(body: AnthropicKeyIn):
        key = body.key.strip()
        if not key:
            _err(ValueError("key is required (use DELETE to remove the stored key)"))
        store.set_setting("anthropic_key", key)
        _, origin = resolve_anthropic_headers(store)
        return {"ok": True, "source": origin}

    @app.delete("/api/settings/anthropic-key")
    async def clear_anthropic_key():
        """Removing the stored key falls back to the deployment's env key when one exists — on a
        hosted trial cell that is the trial key, until its operator removes it too."""
        store.set_setting("anthropic_key", None)
        key, origin = resolve_anthropic_headers(store)
        return {"ok": True, "configured": bool(key), "source": origin}

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
    async def get_gateway():
        return _gateway_status()

    @app.put("/api/settings/gateway")
    async def set_gateway(body: GatewayIn):
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
    async def clear_gateway():
        """Back to the environment's gateway if the deployment set one, else Anthropic."""
        store.set_setting("gateway_url", None)
        store.set_setting("gateway_token", None)
        return {"ok": True, **_gateway_status()}

    # ── model providers (TR-301): the list a cell holds, one of them the default ──────────
    # The Anthropic entry is the key + gateway settings above under another door; OpenAI and
    # OpenAI-compatible entries (LiteLLM, OpenRouter, Ollama, vLLM) live in one JSON setting.
    # Credentials are write-only: the listing says configured-or-not and where from.
    @app.get("/api/settings/providers")
    async def get_providers():
        return providers_mod.list_providers(store)

    @app.put("/api/settings/providers/default")
    async def set_default_provider(body: ProviderDefaultIn):
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
    async def delete_provider(provider_id: str):
        known = {p["id"] for p in providers_mod.list_providers(store)["providers"]}
        if provider_id not in known:
            _err(KeyError(f"unknown provider {provider_id!r}"), 404)
        try:
            providers_mod.delete_provider(store, provider_id)
        except ValueError as e:
            _err(e)
        return {"ok": True, **providers_mod.list_providers(store)}

    # ── agent tracing: where runs are exported, and whether ──────────────────
    # A console-stored value wins over the environment, like the Anthropic key. Secrets (the
    # key, the headers) are write-only: the API says whether one resolves and where from.
    # ── agent limits (TR-325): runs per agent per day, console first, then the environment ──
    def _agent_limits() -> dict:
        from .builtin_agents import DAILY_CAP_ENV, DAILY_RUN_CAP, daily_cap
        cap, source = daily_cap(store)
        return {"daily_cap": cap, "daily_cap_source": source, "default": DAILY_RUN_CAP,
                "env": DAILY_CAP_ENV}

    @app.get("/api/settings/agents")
    async def get_agent_limits():
        return _agent_limits()

    @app.put("/api/settings/agents")
    async def set_agent_limits(body: AgentLimitsIn):
        from .builtin_agents import DAILY_CAP_MAX, DAILY_CAP_SETTING
        raw = body.daily_cap
        if raw is None or str(raw).strip() == "":
            store.set_setting(DAILY_CAP_SETTING, None)
        else:
            try:
                n = int(str(raw).strip())
            except ValueError:
                n = 0
            if not 1 <= n <= DAILY_CAP_MAX:
                _err(ValueError(f"runs per agent per day must be a whole number from 1 to "
                                f"{DAILY_CAP_MAX}"))
            store.set_setting(DAILY_CAP_SETTING, str(n))
        return {"ok": True, **_agent_limits()}

    @app.get("/api/settings/tracing")
    async def get_tracing():
        return tracing_status(store)

    @app.put("/api/settings/tracing")
    async def set_tracing(body: TracingIn):
        """Every field is optional; a field left out is untouched, an empty string clears the
        stored value (the environment value, if any, takes over again)."""
        if body.provider is not None:
            p = body.provider.strip().lower()
            if p and p not in tracing_providers:
                _err(ValueError(f"provider must be one of {', '.join(tracing_providers)}"))
            store.set_setting("tracing_provider", p or None)
        if body.endpoint is not None:
            ep = body.endpoint.strip()
            if ep and not (ep.startswith("http://") or ep.startswith("https://")):
                _err(ValueError("endpoint must be an http(s) URL, e.g. https://collector:4318"))
            store.set_setting("tracing_endpoint", ep or None)
        if body.api_key is not None:
            store.set_setting("tracing_api_key", body.api_key.strip() or None)
        if body.headers is not None:
            store.set_setting("tracing_headers", body.headers.strip() or None)
        if body.enabled is not None:
            store.set_setting("tracing_enabled", "1" if body.enabled else "0")
        out = tracing_status(store)
        if out["enabled"] and not out["active"]:
            out["note"] = "switched on, but no endpoint resolves; pick a provider or set one"
        return {"ok": True, **out}

    # ── the Slack bot token: same contract as the Anthropic key ──────────────
    # One token per instance, behind every slack:// subscription. It is a credential, so it is
    # write-only over the API exactly like the model key: the console can learn THAT one resolves
    # and where from, never what it is.
    @app.get("/api/settings/slack-bot-token")
    async def get_slack_token():
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
    async def set_slack_team(body: SlackTeamIn):
        team_id = body.team_id.strip()
        if not team_id:
            _err(ValueError("team_id is required (use DELETE to clear the team)"))
        store.set_setting(SLACK_TEAM_SETTING,
                          json.dumps({"id": team_id, "name": body.team_name.strip()}))
        return {"ok": True, "team": _slack_team()}

    @app.delete("/api/settings/slack-team")
    async def clear_slack_team():
        store.set_setting(SLACK_TEAM_SETTING, None)
        return {"ok": True, "team": None}

    @app.put("/api/settings/slack-bot-token")
    async def set_slack_token(body: SlackTokenIn):
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
    async def clear_slack_token():
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
    async def slack_channels_changed(body: SlackChannelsChangedIn | None = None):
        """Tares Cloud forwards Slack's channel events here (member_joined_channel, channel_created,
        channel_rename, channel_archive); the next read of the list asks Slack again."""
        _drop_channels_cache()
        return {"ok": True}

    # ── the Slack signing secret: the credential that makes inbound Slack safe ──
    # Same write-only contract as the bot token. Without it POST /api/slack/events answers 503:
    # there is no configuration in which accepting an unverified inbound request is correct.
    @app.get("/api/settings/slack-signing-secret")
    async def get_slack_signing_secret():
        secret, origin = slack_verify.resolve_secret(store)
        return {"configured": bool(secret), "source": origin,
                "stored": bool(store.get_setting(slack_verify.SETTING_KEY)),
                "env_overrides": origin.startswith("env:")}

    @app.put("/api/settings/slack-signing-secret")
    async def set_slack_signing_secret(body: SlackSigningSecretIn):
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
    async def clear_slack_signing_secret():
        store.set_setting(slack_verify.SETTING_KEY, None)
        secret, origin = slack_verify.resolve_secret(store)
        return {"ok": True, "configured": bool(secret), "source": origin}

    # ── inbound Slack: the /tares slash command ───────────────────────────
    # The only route in this daemon that is public to the auth middleware AND accepts a body from
    # the internet, so it carries its own authentication: an HMAC over the raw bytes.
    _ask_cap = slack_mod.AskCap()
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

    async def _slack_answer(question: str, response_url: str, thread_ts: str | None) -> None:
        """Run the Ask agent for one slash command and post the result. Never raises: this runs
        detached from the request, so an exception here would otherwise be pure silence in Slack."""
        from .agent import run_agent
        text, error = "", None
        try:
            provider, key_origin = resolve_provider(store)
            default_pid = providers_mod.default_id(store)
            self_headers = {"Authorization": f"Bearer {AUTH_TOKEN}"} if AUTH_TOKEN else {}
            async def _run():
                nonlocal text, error
                async for chunk in run_agent(provider, [{"role": "user", "content": question}],
                                             model=providers_mod.default_model_for(store, default_pid),
                                             self_headers=self_headers,
                                             on_usage=lambda m, u: _record_ask_usage(
                                                 m, u, key_source=key_origin, kind=provider.kind,
                                                 provider_id=default_pid),
                                             tracer=tracing.tracer_for("ask")):
                    for line in chunk.splitlines():
                        if not line.startswith("data: "):
                            continue
                        ev = json.loads(line[6:])
                        if ev.get("type") == "text":
                            text += ev.get("text") or ""
                        elif ev.get("type") == "error":
                            error = ev.get("detail") or "the assistant failed"
            await asyncio.wait_for(_run(), timeout=SLACK_ASK_TIMEOUT)
        except asyncio.TimeoutError:
            error = f"that took longer than {SLACK_ASK_TIMEOUT}s; try a narrower question"
        except Exception as e:   # noqa: BLE001 — every failure has to reach the user as words
            error = f"{type(e).__name__}: {e}"
        if text.strip():
            # Partial output plus an error still beats an error alone; say both.
            body = slack_mod.build_answer(question, text + (f"\n\n_{error}_" if error else ""),
                                          thread_ts)
        else:
            body = slack_mod.build_error(
                f":warning: {error or 'the assistant returned nothing to say'}", thread_ts)
        await _slack_respond(response_url, body)

    @app.post(SLACK_EVENTS_PATH, include_in_schema=False)
    async def slack_events(request: Request):
        """Slack's inbound endpoint: the URL-verification handshake and `/tares ask …`.

        Two contracts shape this whole handler:

        · **The signature covers the RAW body.** Read `await request.body()` and parse afterwards —
          letting FastAPI decode and re-serialise changes the bytes and every HMAC fails.
        · **Slack allows 3 seconds to ACK.** A model call cannot make that, so anything that has to
          think ACKs immediately and posts the answer to `response_url` from a background task.
          Anything we already know (usage, no key, over the cap) is answered in the ACK itself.
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
            # An interaction: someone clicked a button on a message Tares posted (View in Tares,
            # Open timeline, TR-275). Those buttons are links, so the browser already went where
            # they point; there is nothing to do but ACK. Without a 200 here Slack puts a warning
            # triangle next to the button.
            return Response(status_code=200)
        response_url = (form.get("response_url") or "").strip()
        thread_ts = (form.get("thread_ts") or "").strip() or None
        # With several workspaces on one Slack, Tares Cloud forwards `/tares ask <slug> …` as typed;
        # the slug comes from the connect URL it gave this cell, never from the request.
        question, problem = slack_mod.parse_command(form.get("text", ""), _slack_workspace)
        if problem:
            return slack_mod.build_error(problem, thread_ts)
        if not response_url:
            return slack_mod.build_error(
                ":warning: that request carried no response_url; Tares has nowhere to reply",
                thread_ts)
        if resolve_provider(store)[0] is None:
            return slack_mod.build_error(
                ":warning: no model provider is configured on this Tares instance; add one "
                "under Settings in the console, or set `ANTHROPIC_API_KEY`", thread_ts)
        if not runtime.catalog.sources:
            return slack_mod.build_error(
                ":warning: this Tares instance has no sources configured yet, so there is "
                "nothing to ask about", thread_ts)
        if not _ask_cap.take(form.get("team_id", ""), form.get("user_id", "")):
            return slack_mod.build_error(
                f":warning: you've hit the cap of {_ask_cap.cap} Tares questions in 24h "
                "(TARES_SLACK_DAILY_CAP)", thread_ts)

        task = asyncio.create_task(_slack_answer(question, response_url, thread_ts))
        _ask_tasks.add(task)
        task.add_done_callback(_ask_tasks.discard)
        # The ACK Slack shows while we think; the answer replaces it via response_url.
        ack = {"response_type": "ephemeral", "text": ":hourglass_flowing_sand: asking Tares…"}
        if thread_ts:
            ack["thread_ts"] = thread_ts
        return ack

    # ── management API: activity (what agents saw / what woke them) ──────────
    @app.get("/api/activity/queries")
    async def activity_queries(limit: int = 100):
        return store.list_queries(limit=min(limit, 500))

    @app.get("/api/activity/dispatches")
    async def activity_dispatches(limit: int = 100):
        return store.list_dispatches(limit=min(limit, 500))

    @app.get("/api/activity/dispatches/{dispatch_id}")
    async def activity_dispatch(dispatch_id: str):
        """One firing, deep. Fetch-by-id so a linked dispatch page never dead-ends (unlike the
        capped list). Includes the per-subscriber delivery attempts with masked endpoints + agent
        names, so the failure is attributable to a specific agent."""
        d = store.get_dispatch(dispatch_id)
        if d is None:
            _err(KeyError(f"unknown dispatch {dispatch_id!r}"), 404)
        from .config import agent_name_from_url
        deliveries = []
        for dv in store.deliveries_for(dispatch_id):
            url = dv["url"].rstrip("/")
            name, masked = _agent_identity(url)
            kind = ("tares" if agent_name_from_url(url) is not None
                    else "slack" if slack_channel_from_url(url) is not None else "webhook")
            deliveries.append({"agent": name, "endpoint": masked, "kind": kind, "ok": dv["ok"],
                               "error": dv["error"], "delivered_at": dv["delivered_at"]})
        return {**d, "deliveries": deliveries}

    # ── connected agents: subscriptions grouped by endpoint, named deterministically ──
    _AGENT_ADJ = ["brisk", "quiet", "amber", "bold", "calm", "deft", "eager", "fleet",
                  "keen", "lucid", "merry", "noble", "prime", "swift", "vivid", "wry"]
    _AGENT_NOUN = ["heron", "otter", "drake", "skiff", "beacon", "compass", "gull", "harbor",
                   "keel", "lantern", "mast", "pilot", "rudder", "sextant", "tide", "wake"]

    def _agent_identity(url: str) -> tuple[str, str]:
        """(name, masked display URL) for a subscriber endpoint. A Tares agent's URL carries its
        real name and no secret, so it's shown verbatim; a Slack channel likewise. An external hook
        URL is the identity but carries a secret in the path, so it's anonymized (deterministic
        name) and the last path segment masked."""
        from .config import agent_name_from_url
        internal = agent_name_from_url(url.rstrip("/"))
        if internal is not None:
            return internal, "in-process (Tares agent)"
        channel = slack_channel_from_url(url.rstrip("/"))
        if channel is not None:
            # The channel IS the identity and holds no secret (the token does), so it's shown as
            # written — a raw slack://channel/C0123456 URL in the roster reads like a bug.
            return f"#{channel}", "Slack channel"
        norm = url.rstrip("/")
        h = int(hashlib.sha256(norm.encode()).hexdigest(), 16)
        name = f"{_AGENT_ADJ[h % len(_AGENT_ADJ)]}-{_AGENT_NOUN[(h // 16) % len(_AGENT_NOUN)]}"
        try:
            from urllib.parse import urlsplit
            u = urlsplit(norm)
            segs = [seg for seg in u.path.split("/") if seg]
            if segs:
                segs[-1] = segs[-1][:2] + "…" if len(segs[-1]) > 2 else "…"
            masked = f"{u.scheme}://{u.netloc}/" + "/".join(segs)
        except Exception:
            masked = norm[:24] + "…"
        return name, masked

    @app.get("/api/agents")
    async def agent_roster():
        """The roster of everything a trigger wakes: Tares agents (run in-process) and connected
        external agents (webhooks), one row each, tagged by `kind`. Both are subscriptions, so both
        appear here identically — wiring (triggers), delivery health, recent wakes."""
        stats = store.delivery_stats()
        agents: dict[str, dict] = {}
        from .config import agent_url as _agent_url
        # a Tares agent is woken by the projects' wiring (P-TR-216): each wake-up that is on reads
        # as its subscription here, so the roster shows Tares and external agents alike
        wired = [{"subscription_id": f"wire:{w['agent']}", "trigger": w["trigger"],
                  "url": _agent_url(w["agent"]), "created_at": None, "project": w["project"],
                  "created_by": "tares"}
                 for w in store.list_wakes() if w["enabled"]]
        for sub in [*store.all_subscriptions(), *wired]:
            norm = sub["url"].rstrip("/")
            a = agents.get(norm)
            if a is None:
                from .config import agent_name_from_url
                name, masked = _agent_identity(norm)
                kind = ("tares" if agent_name_from_url(norm) is not None
                        else "slack" if slack_channel_from_url(norm) is not None
                        else "connected")
                st = stats.get(sub["url"], stats.get(norm, {}))
                a = agents[norm] = {
                    "name": name, "endpoint": masked, "kind": kind,
                    "subscriptions": [], "triggers": [], "created_by": set(),
                    "first_seen": sub["created_at"],
                    # windowed counts are what the roster shows (an all-time total next to a
                    # "last woken" of weeks ago reads as busy); totals ride along for context.
                    "delivered_ok_24h": st.get("ok", 0), "delivered_fail_24h": st.get("fail", 0),
                    "delivered_ok_total": st.get("ok_total", 0),
                    "delivered_fail_total": st.get("fail_total", 0),
                    "pending": st.get("pending", 0), "last_woken": st.get("last_at"),
                    # currently failing: the most recent delivery to this endpoint did not succeed —
                    # deliberately over ALL deliveries, so a failing endpoint that has gone quiet
                    # doesn't quietly go healthy when it falls out of the window.
                    "unhealthy": st.get("fail_total", 0) > 0 and not st.get("last_ok", True),
                    "last_error": None if st.get("last_ok", True) else st.get("last_error"),
                    "recent": [{"at": d["at"], "ok": d["ok"], "trigger": d["trigger"],
                                "key": d["key"], "error": d["error"], "dispatch_id": d["dispatch_id"]}
                               for d in store.recent_deliveries(sub["url"], 10)],
                }
            a["subscriptions"].append({"subscription_id": sub["subscription_id"],
                                       "trigger": sub["trigger"], "created_at": sub["created_at"],
                                       # a subscription to a whole project has no one trigger
                                       "project": sub.get("project")})
            if sub["trigger"] and sub["trigger"] not in a["triggers"]:
                a["triggers"].append(sub["trigger"])
            if sub["created_by"]:
                a["created_by"].add(sub["created_by"])
            if sub["created_at"] and (a["first_seen"] is None or sub["created_at"] < a["first_seen"]):
                a["first_seen"] = sub["created_at"]
        out = []
        for a in agents.values():
            a["created_by"] = sorted(a["created_by"])
            out.append(a)
        out.sort(key=lambda x: str(x.get("last_woken") or x.get("first_seen") or ""), reverse=True)
        return {"agents": out}

    @app.get("/api/subscriptions")
    async def subscriptions():
        return store.list_all_subscriptions()

    # ── management API: projects (templates instantiated with params) ─────────
    # A project creates and owns ordinary objects; those stay editable and deletable on their own
    # pages. These routes are the instance's lifecycle and its combined view.
    def _uc_err(e: Exception):
        if isinstance(e, KeyError):
            _err(e, 404)
        if isinstance(e, (ProjectError, CatalogError, ValueError)):
            _err(e, 400)
        raise e

    @app.get("/api/projects/templates")
    async def project_templates():
        return {"templates": projects.list_templates()}

    # By key, hidden templates included: the gallery list leaves out templates a person does not
    # pick (rius_rca is created by the Rius control plane), but the Edit page of a project built
    # on one still has to render its form.
    @app.get("/api/projects/templates/{key}")
    async def project_template(key: str):
        from .projects.registry import get_template
        try:
            return get_template(key).describe()
        except ProjectError:
            _err(KeyError(f"unknown template {key!r}"), 404)

    def _project_card(uid: str) -> dict:
        """What a project key is told about its project: who it is and what it is made of, not
        its template parameters (they can hold credentials)."""
        p = store.get_project(uid) or {}
        names, _scope = _project_view(uid)
        return {"id": uid, "name": p.get("name"), "template": p.get("template"),
                "goal": p.get("goal"),
                "status": p.get("status"), "created_at": p.get("created_at"),
                "sources": names,
                "triggers": sorted(t.name for t in runtime.catalog.triggers
                                   if uid in store.projects_using("trigger", t.name)),
                "skills": [s["name"] for s in store.list_skills(uid)]}

    @app.get("/api/projects")
    async def list_projects(request: Request):
        pk = _pk(request)
        if pk:
            return {"projects": [_project_card(pk)]}
        return {"projects": projects.list()}

    @app.post("/api/projects", status_code=201)
    async def create_project(body: ProjectIn):
        try:
            params = {**body.params, "objects": body.objects} if body.objects is not None else body.params
            return projects.create(body.template, params, name=body.name or None, goal=body.goal)
        except Exception as e:
            _uc_err(e)

    @app.get("/api/projects/{uid}")
    async def get_project(uid: str, request: Request):
        if _pk(request):
            return _project_card(uid)   # the guard already held it to the key's own project
        inst = projects.get(uid)
        if inst is None:
            _err(KeyError(f"unknown project {uid!r}"), 404)
        return inst

    @app.put("/api/projects/{uid}")
    async def update_project(uid: str, body: ProjectUpdate):
        given = body.model_fields_set
        try:
            if "goal" in given and not ({"params", "objects"} & given) and not body.name.strip():
                return projects.set_goal(uid, body.goal)
            params = {**body.params, "objects": body.objects} if body.objects is not None else body.params
            out = projects.update(uid, params)
            if "goal" in given:
                projects.set_goal(uid, body.goal)
                out = {**projects.get(uid), "report": out["report"]}
            if body.name.strip():
                store.update_project(uid, name=body.name.strip())
                out = {**projects.get(uid), "report": out["report"]}
            return out
        except Exception as e:
            _uc_err(e)

    @app.delete("/api/projects/{uid}")
    async def delete_project(uid: str, purge_events: bool = False, delete_sources: str = ""):
        """Deletes the project's triggers, agents and MCP servers. `delete_sources=a,b` also
        deletes those of its sources that no other project uses (the others are kept and listed
        in `kept`); every other source stays, in the default project if in no other. Sources are
        only ever deleted when asked: `delete_sources=all` names every source of the project, and
        `none`, or leaving it out, keeps them all."""
        chosen = [x.strip() for x in delete_sources.split(",") if x.strip() and x.strip() != "none"]
        if chosen == ["all"]:
            chosen = sorted(store.project_sources(uid))
        try:
            return projects.delete(uid, purge_events=purge_events, delete_sources=chosen)
        except Exception as e:
            _uc_err(e)

    @app.post("/api/projects/{uid}/sources")
    async def add_project_source(uid: str, body: dict = Body(...)):
        """Add an existing source to a project (sources are shared between projects)."""
        name = str((body or {}).get("name") or "").strip()
        if not name:
            _err(ValueError("name is required"), 400)
        try:
            return projects.add_source(uid, name)
        except Exception as e:
            _uc_err(e)

    @app.delete("/api/projects/{uid}/sources/{name}")
    async def remove_project_source(uid: str, name: str):
        """Take a source out of a project; refused while a trigger of the project reads it."""
        try:
            return projects.remove_source(uid, name)
        except Exception as e:
            _uc_err(e)

    @app.post("/api/projects/{uid}/pause")
    async def pause_project(uid: str, body: dict | None = Body(default=None)):
        """Body {"sources": true} also pauses the project's sources, so nothing accumulates while
        it is paused (the AI SRE demo's traffic, for one). Default: sources keep ingesting."""
        try:
            return projects.pause(uid, sources=bool((body or {}).get("sources")))
        except Exception as e:
            _uc_err(e)

    @app.post("/api/projects/{uid}/resume")
    async def resume_project(uid: str):
        try:
            return projects.resume(uid)
        except Exception as e:
            _uc_err(e)

    @app.post("/api/projects/{uid}/repair")
    async def repair_project(uid: str, body: ProjectRepair):
        try:
            return projects.repair(uid, body.key)
        except Exception as e:
            _uc_err(e)

    @app.post("/api/projects/templates/{key}/detect")
    async def project_detect(key: str):
        try:
            return await projects.detect(key)
        except Exception as e:
            _uc_err(e)

    @app.post("/api/projects/{uid}/actions/{name}")
    async def project_action(uid: str, name: str, request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return await asyncio.to_thread(projects.action, uid, name, body if isinstance(body, dict) else {})
        except Exception as e:
            _uc_err(e)

    @app.get("/api/projects/{uid}/timeline")
    async def project_timeline(request: Request, uid: str, limit: int = 50, before: str = "",
                               trigger: str = "", agent: str = "", outcome: str = "",
                               entity: str = ""):
        """Everything that happened in the project, newest first, one thread per firing or
        unprompted run, with what each led to nested inside (TR-331). Page with `before` =
        the previous page's `next_before`."""
        if projects.get(uid) is None:
            _err(KeyError(f"unknown project {uid!r}"), 404)
        at = None
        if before:
            try:
                at = datetime.fromisoformat(before.replace("Z", "+00:00"))
            except ValueError:
                _err(ValueError("before must be an ISO timestamp, as next_before gives it"))
        if outcome and outcome not in timeline.OUTCOMES:
            _err(ValueError(f"outcome must be one of {', '.join(timeline.OUTCOMES)}"))
        scheduled = {t.name for t in runtime.catalog.triggers
                     if getattr(t.condition, "every", None)}
        out = await asyncio.to_thread(
            timeline.project_timeline, store, uid, limit=limit, before=at, trigger=trigger,
            agent=agent, outcome=outcome, entity=entity, scheduled=scheduled)
        if _pk(request):
            # a webhook URL can carry its receiver's secret: a project key sees it masked,
            # like the agents roster shows it
            def mask(threads):
                for t in threads:
                    for d in t.get("deliveries") or []:
                        if d.get("kind") == "webhook":
                            d["target"] = _agent_identity(d["target"] or "")[1]
                    for r in t.get("runs") or []:
                        stack = [r]
                        while stack:
                            x = stack.pop()
                            mask(x.get("firings") or [])
                            stack.extend(x.get("children") or [])
            mask(out["threads"])
        return out

    # ── the goal-first project page: how it works, what the agents found, is it working ──
    def _scheduled() -> set:
        return {t.name for t in runtime.catalog.triggers if getattr(t.condition, "every", None)}

    @app.get("/api/projects/{uid}/outline")
    async def project_outline(uid: str):
        """How the project works, in plain sentences built from its configuration."""
        _project_or_404(uid)
        return await asyncio.to_thread(goal_mod.outline, store, runtime.catalog, uid,
                                       runtime.health_snapshot())

    @app.get("/api/projects/{uid}/results")
    async def project_results(uid: str, limit: int = 20, before: str = "", show: str = ""):
        """What the agents concluded, newest first: one result per chain of runs. Page with
        `before` = the previous page's `next_before`. `show`: "findings" leaves out the runs that
        had nothing to report, "agent:<name>" keeps that agent's conclusions."""
        _project_or_404(uid)
        at = None
        if before:
            try:
                at = datetime.fromisoformat(before.replace("Z", "+00:00"))
            except ValueError:
                _err(ValueError("before must be an ISO timestamp, as next_before gives it"))
        if show and show != "findings" and not show.startswith("agent:"):
            _err(ValueError('show is "findings" or "agent:<name>"'))
        return await asyncio.to_thread(goal_mod.project_results, store, uid, limit=limit,
                                       before=at, scheduled=_scheduled(), show=show)

    @app.get("/api/projects/{uid}/results/{run_id}")
    async def project_result(uid: str, run_id: str):
        """One result with its full note and the steps that led to it."""
        _project_or_404(uid)
        try:
            return await asyncio.to_thread(goal_mod.result_detail, store, runtime.catalog, uid,
                                           run_id, _scheduled())
        except KeyError as e:
            _err(e, 404)

    @app.post("/api/projects/{uid}/results/{run_id}/handled")
    async def project_result_handled(uid: str, run_id: str, request: Request,
                                     body: dict = Body(...)):
        """{"handled": true|false}: mark a result handled by the caller, or clear the mark."""
        _project_or_404(uid)
        run = store.get_agent_run(run_id)
        if run is None or uid not in store.run_projects(run_id):
            _err(KeyError(f"project has no run {run_id!r}"), 404)
        handled = (body or {}).get("handled")
        if not isinstance(handled, bool):
            _err(ValueError("handled must be true or false"))
        ident = getattr(request.state, "credential", None)
        store.set_run_handled(run_id, (ident or {}).get("name") or "console" if handled else None)
        run = store.get_agent_run(run_id)
        return {"ok": True, "id": run_id,
                "handled": ({"at": run["handled_at"], "by": run["handled_by"]}
                            if run.get("handled_at") else None)}

    @app.get("/api/projects/{uid}/health")
    async def project_health(uid: str):
        """Is the project working: a state, one message, and what needs attention first."""
        p = _project_or_404(uid)
        return await asyncio.to_thread(goal_mod.project_health, store, runtime.catalog, uid, p,
                                       runtime.health_snapshot())

    @app.get("/api/projects/{uid}/summary")
    async def project_summary(uid: str):
        try:
            return projects.summary(uid)
        except Exception as e:
            _uc_err(e)

    # ── the guided project setup: goal -> plan -> apply -> connect -> try it ──
    from . import setup_flow

    def _setup_err(e):
        raise HTTPException(status_code=e.status, detail=e.message)

    def _setup_model():
        """(provider, model, key origin, provider id) the plan is written on: the cell's
        default provider, exactly as Ask resolves it. 409 with a plain message when none."""
        provider, origin = resolve_provider(store)
        if provider is None:
            _err(ValueError(setup_flow.NO_PROVIDER), 409)
        pid = providers_mod.default_id(store)
        return provider, providers_mod.default_model_for(store, pid), origin, pid

    async def _setup_read(name: str, args: dict) -> tuple[bool, str]:
        """The planner's read tools, answered by this daemon's own routes in process."""
        paths = {"list_connectors": "/api/connectors", "list_sources": "/api/sources",
                 "list_templates": "/api/projects/templates"}
        if name == "github_repo":
            return await setup_flow.read_github_repo(store, str(args.get("repo") or ""))
        if name == "source_fields":
            path = f"/api/sources/{str(args.get('name') or '')}/fields"
        elif name in paths:
            path = paths[name]
        else:
            return False, f"unknown tool {name!r}"
        headers = {"Authorization": f"Bearer {AUTH_TOKEN}"} if AUTH_TOKEN else {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://setup", headers=headers, timeout=30) as cx:
            r = await cx.get(path)
        text = r.text
        return r.status_code < 400, text if len(text) <= 20000 else text[:20000] + "\n(truncated)"

    async def _setup_generate(message: str, prev: dict | None = None,
                              who: str | None = None) -> dict:
        provider, model, origin, pid = _setup_model()
        try:
            return await setup_flow.model_plan(
                provider, model, message, store, runtime.catalog, _setup_read,
                tracer=tracing.tracer_for("project-setup"),
                on_usage=lambda u: _record_ask_usage(model, u, key_source=origin,
                                                     kind=provider.kind, provider_id=pid),
                prev=prev, who=who)
        except setup_flow.SetupError as e:
            _setup_err(e)

    @app.get("/api/setup/github-suggestion")
    async def setup_github_suggestion():
        """The guided setup's opening line when GitHub is connected (TR-262): one goal grounded
        in the person's newest repositories, cached a day. {available: false} without GitHub;
        {suggestion: null} when no model provider is set up (the page just leaves it out)."""
        if not store.list_github_credentials():
            return {"available": False, "suggestion": None, "repos": []}
        provider, _origin = resolve_provider(store)
        if provider is None:
            return {"available": True, "suggestion": None,
                    "repos": await setup_flow.github_repo_lines(store, limit=8)}
        pid = providers_mod.default_id(store)
        model = providers_mod.default_model_for(store, pid)
        try:
            out = await setup_flow.github_suggestion(
                store, provider, model,
                on_usage=lambda u: _record_ask_usage(model, u, key_source=_origin,
                                                     kind=provider.kind, provider_id=pid))
        except Exception as e:  # noqa: BLE001 — the page works without it
            print(f"setup: github suggestion failed: {type(e).__name__}: {e}")
            return {"available": True, "suggestion": None, "repos": []}
        return {"available": True, **out}

    @app.post("/api/setup/plan")
    async def setup_plan(body: dict = Body(...)):
        """{goal, who?, existing_sources?} -> {plan}: the whole project in plain words, written
        by the cell's model and checked like a catalog import."""
        try:
            goal = goal_mod.normalize_goal(body.get("goal"))
        except ValueError as e:
            _err(e)
        if not goal:
            _err(ValueError("say what the project is for"))
        who = body.get("who") or None
        if who not in (None, "tares", "own"):
            _err(ValueError("who is tares or own"))
        gh_repos = await setup_flow.github_repo_lines(store)
        plan = await _setup_generate(setup_flow.plan_message(
            goal, who, body.get("existing_sources", True) is not False, store, runtime.catalog,
            github=gh_repos), who=who)
        return {"plan": plan}

    @app.post("/api/setup/adjust")
    async def setup_adjust(body: dict = Body(...)):
        """{plan, instruction} -> {plan}: the plan revised as the person asked."""
        plan = body.get("plan")
        instruction = " ".join(str(body.get("instruction") or "").split())
        if not isinstance(plan, dict):
            _err(ValueError("plan is required"))
        if not instruction:
            _err(ValueError("say what to change"))
        if len(instruction) > 2000:
            _err(ValueError("an instruction is at most 2000 characters"))
        return {"plan": await _setup_generate(setup_flow.adjust_message(plan, instruction),
                                              prev=plan)}

    @app.post("/api/setup/check")
    async def setup_check(body: dict = Body(...)):
        """{plan} -> {plan, problems}: the plan normalized exactly as apply would (sentences and
        the summary derived from it as it is now) and what stops it from applying, per item
        ({where, message}), in plain words. No model call; apply accepts a plan with no
        problems."""
        raw = body.get("plan")
        if not isinstance(raw, dict):
            _err(ValueError("plan is required"))
        plan, problems = setup_flow.check(raw, store, runtime.catalog,
                                          draft=body.get("project") or None)
        return {"plan": plan, "problems": problems}

    # ── drafts: a project being planned lives on the cell from the first "Plan it" ─────────
    # The draft is a project row with status draft and nothing in it; its setup holds the goal,
    # who does the work, the plan as the person edits it, and `planning`: the steps of a plan
    # being written right now ({state running|failed, steps [{text, state, at}], error}).
    # Planning runs in the background; the console polls GET setup to show it.
    _planning_tasks: dict[str, asyncio.Task] = {}

    def _draft_or_404(uid: str) -> dict:
        p = _project_or_404(uid)
        if p.get("status") != setup_flow.DRAFT:
            _err(ValueError("this project is already set up"), 409)
        return store.get_project_setup(uid) or {}

    def _draft_words(body: dict, setup: dict | None = None) -> tuple[str, str]:
        """The name the person gave the project and what they said about it, both optional; an
        absent field keeps what the draft has."""
        setup = setup or {}
        name = " ".join(str(body["name"] if "name" in body else setup.get("name") or "").split())
        about = str(body["description"] if "description" in body
                    else setup.get("description") or "").strip()
        if len(name) > 80:
            _err(ValueError("a project name is at most 80 characters"))
        if len(about) > 4000:
            _err(ValueError("a description is at most 4000 characters"))
        return name, about

    def _draft_name(goal: str) -> str:
        base = (goal[:60].rsplit(" ", 1)[0] if len(goal) > 60 else goal) or "New project"
        base = base[:1].upper() + base[1:]
        taken = {p["name"] for p in store.list_projects()}
        name, n = base, 2
        while name in taken:
            name, n = f"{base} {n}", n + 1
        return name

    def _start_planning(uid: str, message: str, prev: dict | None, who: str | None) -> None:
        """Write (or revise) the draft's plan in the background, its steps kept on the setup."""
        steps: list[dict] = []

        def save(**planning) -> None:
            if planning.get("state") == "failed":   # the step it was on stopped there
                for st in steps:
                    if st["state"] == "running":
                        st["state"] = "stopped"
            setup = store.get_project_setup(uid)
            if setup is None:            # the draft was deleted meanwhile
                return
            setup["planning"] = planning or None
            store.set_project_setup(uid, setup)

        def progress(text: str, running: bool) -> None:
            for st in steps:
                if st["state"] == "running":
                    st["state"] = "done"
            steps.append({"text": text, "state": "running" if running else "done",
                          "at": now_utc().isoformat()})
            save(state="running", steps=list(steps))

        async def run() -> None:
            try:
                provider, model, origin, pid = _setup_model()
                plan = await setup_flow.model_plan(
                    provider, model, message, store, runtime.catalog, _setup_read,
                    tracer=tracing.tracer_for("project-setup"),
                    on_usage=lambda u: _record_ask_usage(model, u, key_source=origin,
                                                         kind=provider.kind, provider_id=pid),
                    prev=prev, who=who, progress=progress)
                setup = store.get_project_setup(uid)
                if setup is None:
                    return
                # the name the person gave stays the name of a plan written from the goal; a
                # change they asked for afterwards ("call it Bar") wins, and becomes their name
                if setup.get("name") and prev is None:
                    plan["name"] = setup["name"]
                elif setup.get("name") and plan.get("name"):
                    setup["name"] = plan["name"]
                # a secret typed into a new source stays out of the draft (as on every save)
                setup.update(plan=setup_flow.stored_plan(plan), planning=None, who=plan.get("who"))
                store.set_project_setup(uid, setup)
                other = store.get_project_by_name(plan.get("name") or "")
                if plan.get("name") and (other is None or other["id"] == uid):
                    store.update_project(uid, name=plan["name"])   # the list shows the plan's name
            except HTTPException as e:
                save(state="failed", steps=steps, error=str(e.detail),
                     no_provider=e.status_code == 409)
            except setup_flow.SetupError as e:
                save(state="failed", steps=steps, error=e.message)
            except Exception as e:  # noqa: BLE001 — a failed plan is said on the page, not lost
                save(state="failed", steps=steps, error=f"Planning stopped: {type(e).__name__}: {e}")
            finally:
                _planning_tasks.pop(uid, None)

        progress(setup_flow.context_words(runtime.catalog), False)
        _planning_tasks[uid] = asyncio.create_task(run())

    # a restart ends any planning in flight: say so rather than leave the page waiting
    for _p in store.list_projects():
        if _p.get("status") == setup_flow.DRAFT:
            _s = store.get_project_setup(_p["id"]) or {}
            if (_s.get("planning") or {}).get("state") == "running":
                _s["planning"] = {**_s["planning"], "state": "failed",
                                  "error": "Tares restarted while planning. Plan it again."}
                store.set_project_setup(_p["id"], _s)

    @app.post("/api/setup/drafts", status_code=202)
    async def setup_draft(body: dict = Body(...)):
        """{goal, who?, name?, description?} -> {project}: a draft project, and its plan being
        written in the background. GET /api/projects/{id}/setup shows the planning step by step,
        then the plan. `name` is kept as the project's name; `description` goes to the planner."""
        try:
            goal = goal_mod.normalize_goal(body.get("goal"))
        except ValueError as e:
            _err(e)
        if not goal:
            _err(ValueError("say what the project is for"))
        who = body.get("who") or None
        if who not in (None, "tares", "own"):
            _err(ValueError("who is tares or own"))
        name, about = _draft_words(body)
        if name and store.get_project_by_name(name):
            _err(ValueError(f"a project named {name!r} already exists"), 409)
        _setup_model()   # no provider: 409 now, on the goal page, not later on the draft
        uid = "uc_" + uuid.uuid4().hex[:10]
        store.create_project(uid, "custom", name or _draft_name(goal), {"objects": []},
                             status=setup_flow.DRAFT, goal=goal)
        store.log_project(uid, "draft", "planning from the goal")
        store.set_project_setup(uid, {"step": "plan", "goal": goal, "who": who, "plan": None,
                                      "practice_run": None, "name": name, "description": about})
        _start_planning(uid, setup_flow.plan_message(goal, who, True, store, runtime.catalog,
                                                     github=await setup_flow.github_repo_lines(store),
                                                     name=name, description=about),
                        None, who)
        return {"project": projects.get(uid)}

    @app.post("/api/projects/{uid}/setup/plan", status_code=202)
    async def setup_replan(uid: str, body: dict = Body(default={})):
        """A draft planned again in the background: {goal, who?, name?, description?} plans from a
        changed goal; {instruction} changes the current plan as asked."""
        setup = _draft_or_404(uid)
        if uid in _planning_tasks:
            _err(ValueError("Tares is planning this project already"), 409)
        instruction = " ".join(str(body.get("instruction") or "").split())
        if instruction:
            if len(instruction) > 2000:
                _err(ValueError("an instruction is at most 2000 characters"))
            plan = body.get("plan") if isinstance(body.get("plan"), dict) else setup.get("plan")
            if not isinstance(plan, dict):
                _err(ValueError("there is no plan to change yet"))
            _setup_model()
            # a secret typed into a new source is neither kept on the draft nor sent to the model
            plan = setup_flow.stored_plan(plan)
            setup["plan"] = plan
            store.set_project_setup(uid, setup)
            _start_planning(uid, setup_flow.adjust_message(plan, instruction), plan, None)
            return {"ok": True}
        try:
            goal = goal_mod.normalize_goal(body.get("goal") or setup.get("goal"))
        except ValueError as e:
            _err(e)
        if not goal:
            _err(ValueError("say what the project is for"))
        who = body.get("who") or setup.get("who") or None
        if who not in (None, "tares", "own"):
            _err(ValueError("who is tares or own"))
        name, about = _draft_words(body, setup)
        other = store.get_project_by_name(name) if name else None
        if other and other["id"] != uid:
            _err(ValueError(f"a project named {name!r} already exists"), 409)
        _setup_model()
        setup.update(goal=goal, who=who, plan=None, name=name, description=about)
        store.set_project_setup(uid, setup)
        store.update_project(uid, goal=goal, **({"name": name} if name else {}))
        _start_planning(uid, setup_flow.plan_message(goal, who, True, store, runtime.catalog,
                                                     github=await setup_flow.github_repo_lines(store),
                                                     name=name, description=about),
                        None, who)
        return {"ok": True}

    @app.post("/api/setup/apply", status_code=201)
    async def setup_apply(request: Request, body: dict = Body(...)):
        """{plan, project?} -> {project, plan, connect}: create the project and everything it
        needs in one go, then say what only the person can do. `project`: the draft that becomes
        the project. `plan` is the plan as the project keeps it (a secret typed into a new
        source's settings left out)."""
        raw = body.get("plan")
        if not isinstance(raw, dict):
            _err(ValueError("plan is required"))
        draft = body.get("project") or None
        if draft is not None:
            _draft_or_404(str(draft))
            if str(draft) in _planning_tasks:
                _err(ValueError("Tares is still planning this project"), 409)
        plan, errors = setup_flow.normalize(raw, store, runtime.catalog, draft=draft)
        if errors:
            _err(ValueError("This plan cannot be set up: " + " ".join(errors)), 422)
        try:
            uid, key = setup_flow.apply(store, projects, plan, _make_key, draft=draft)
        except setup_flow.SetupError as e:
            _setup_err(e)
        setup = {"plan": setup_flow.stored_plan(plan), "step": "connect", "practice_run": None}
        if key is not None:
            setup["own_key_id"] = key["id"]
        store.set_project_setup(uid, setup)
        base = setup_flow.public_base(str(request.base_url))
        return {"project": projects.get(uid), "plan": setup["plan"],
                "connect": setup_flow.connect_info(store, runtime.catalog, uid, plan, base, key)}

    def _setup_or_404(uid: str) -> dict:
        _project_or_404(uid)
        setup = store.get_project_setup(uid)
        if setup is None:
            _err(KeyError("this project was not set up with the guided setup"), 404)
        return setup

    @app.get("/api/projects/{uid}/setup")
    async def get_project_setup(uid: str, request: Request):
        """{step, plan, practice_run, checks, connect}: where the guided setup is, live status for
        Connect, and what Connect shows (the addresses as the daemon sees them; the own agent's
        key is not in it, it was shown once)."""
        setup = _setup_or_404(uid)
        if _project_or_404(uid).get("status") == setup_flow.DRAFT:
            return {"step": "plan", "draft": True, "goal": setup.get("goal"),
                    "name": setup.get("name") or "", "description": setup.get("description") or "",
                    "who": setup.get("who"), "plan": setup.get("plan"),
                    "planning": setup.get("planning"), "practice_run": None, "checks": None,
                    "connect": None}
        checks = await asyncio.to_thread(setup_flow.checks, store, runtime.catalog,
                                         runtime.health_snapshot(), uid, setup)
        base = setup_flow.public_base(str(request.base_url))
        return {"step": setup.get("step"), "plan": setup.get("plan"),
                "practice_run": setup.get("practice_run"), "checks": checks,
                "connect": setup_flow.connect_info(store, runtime.catalog, uid,
                                                   setup.get("plan") or {}, base, None)}

    @app.put("/api/projects/{uid}/setup")
    async def put_project_setup(uid: str, body: dict = Body(...)):
        """{step}: connect, try or done. On a draft, {plan}: the plan as the person edited it,
        kept so they can leave and come back."""
        setup = _setup_or_404(uid)
        if _project_or_404(uid).get("status") == setup_flow.DRAFT:
            if not isinstance(body.get("plan"), dict):
                _err(ValueError("a draft keeps its plan: send {plan}"))
            if uid in _planning_tasks:
                _err(ValueError("Tares is planning this project; wait for the plan"), 409)
            setup["plan"] = setup_flow.stored_plan(body["plan"])
            if setup.get("name") and body["plan"].get("name"):   # renamed on the plan: that is their name now
                setup["name"] = str(body["plan"]["name"])
            store.set_project_setup(uid, setup)
            return {"ok": True}
        step = body.get("step")
        if step not in setup_flow.STEPS:
            _err(ValueError(f"step is one of {', '.join(setup_flow.STEPS)}"))
        setup["step"] = step
        store.set_project_setup(uid, setup)
        return {"ok": True, "step": step}

    def _plan_watch(setup: dict, name: str) -> dict:
        w = next((w for w in (setup.get("plan") or {}).get("watches") or []
                  if w.get("name") == name), None)
        if w is None:
            _err(KeyError(f"the plan has no source named {name!r}"), 404)
        return w

    async def _practice_ingest(name: str, sample: dict) -> list:
        """Store the example event on the source, labelled practice=true, without waking any
        trigger: a practice event must not start a real run."""
        from .connectors import build_connector
        cfg = runtime.catalog.sources[name]
        envs = build_connector(cfg, store).map_payload(sample)
        for e in envs:
            e.labels = {**(e.labels or {}), "practice": "true"}
        await asyncio.to_thread(store.append, envs)
        rt = runtime.sources.get(name)
        if rt is not None:
            rt.health.events_since_start += len(envs)
            rt.health.last_ok_at = now_utc()
        _metrics.events_ingested(name, len(envs))
        return envs

    @app.post("/api/projects/{uid}/setup/test-event")
    async def setup_test_event(uid: str, body: dict = Body(...)):
        """{source}: send the plan's example event into a push source, so the person sees it
        arrive without wiring anything."""
        setup = _setup_or_404(uid)
        name = str(body.get("source") or "").strip()
        w = _plan_watch(setup, name)
        cfg = runtime.catalog.sources.get(name)
        if cfg is None:
            _err(KeyError(f"source {name!r} no longer exists"), 404)
        if SPECS.get(cfg.connector, {}).get("mode") != "push":
            _err(ValueError(f"{name} collects its events itself, so there is no test event to "
                            "send"))
        if not isinstance(w.get("sample"), dict):
            _err(ValueError(f"the plan has no example event for {name}"))
        _refuse_if_full()
        envs = await _practice_ingest(name, w["sample"])
        return {"ok": True, "ingested": len(envs)}

    async def _practice_input(uid: str, setup: dict, trig) -> tuple[str, str]:
        """(entity, timeline) a practice run or firing starts from: the example event's entity
        (sent now when the source has nothing for it), else the newest event the wake-up's
        filters let through, else the newest event's."""
        from .connectors import build_connector
        from .reads import parse_window, resolve_sources_full
        plan = setup.get("plan") or {}
        window = trig.emit.get("context_window") or "15m"
        key = None
        for w in plan.get("watches") or []:
            cfg = runtime.catalog.sources.get(w.get("name"))
            if (w.get("name") in trig.sources and isinstance(w.get("sample"), dict)
                    and cfg is not None and SPECS.get(cfg.connector, {}).get("mode") == "push"):
                envs = build_connector(cfg, store).map_payload(w["sample"])
                if not envs:
                    continue
                env = envs[0]
                key = (env.labels or {}).get(trig.key_field) if trig.key_field else None
                key = str(key or env.key_value or "")
                _p, count, _rows = resolve_sources_full(store, trig.sources, trig.name, key=key,
                                                        window=window, filters=trig.filters)
                if not count:
                    await _practice_ingest(w["name"], w["sample"])
                break
        if not key:
            # the newest event the wake-up would count (its filters) in the last day, with the
            # timeline reaching back to it
            hit = store.newest_key(list(trig.sources), trig.filters, now_utc() - timedelta(days=1))
            if hit:
                key, at = hit
                at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
                age_m = int((now_utc() - at).total_seconds() // 60) + 2
                if age_m > parse_window(window).total_seconds() / 60:
                    window = f"{age_m}m"
        if not key:
            for s in trig.sources:
                ev = store.recent_events(source=s, limit=1)
                if ev and ev[0].get("key"):
                    key = ev[0]["key"]
                    break
        if not key:
            _err(ValueError("Nothing has arrived yet to practice on. Send a test event first."),
                 409)
        payload, _n, _rows = resolve_sources_full(store, trig.sources, trig.name, key=key,
                                                  window=window, filters=trig.filters)
        return key, payload

    @app.post("/api/projects/{uid}/setup/practice")
    async def setup_practice(uid: str):
        """A practice run ("Run it once now"): the first agent that looks runs once on the example event's entity
        ({run_id}), or, for the person's own agent, a practice firing goes to the project's
        subscriptions ({dispatch_id}). Practice results are shown, never counted."""
        setup = _setup_or_404(uid)
        plan = setup.get("plan") or {}
        triggers = sorted((t for t in runtime.catalog.triggers
                           if uid in store.projects_using("trigger", t.name)),
                          key=lambda t: t.name)
        if not triggers:
            _err(ValueError("this project has no trigger to practice with"), 409)
        if plan.get("who") == "own":
            trig = triggers[0]
            key, payload = await _practice_input(uid, setup, trig)
            subs = store.list_project_subscriptions(uid)
            dispatch_id = uuid.uuid4().hex
            kind = trig.emit.get("kind", trig.name)
            body = {"dispatch_id": dispatch_id, "trigger": trig.name, "project": uid,
                    "kind": kind, "key": key, "fired_at": now_utc().isoformat(),
                    "payload": payload, "run_ids": [], "practice": True}
            delivered = 0
            for s in subs:
                ok, error = await dispatcher._post(s["url"], body, attempts=1)
                store.log_delivery(dispatch_id, s["subscription_id"], s["url"], ok, error)
                delivered += 1 if ok else 0
            store.log_dispatch(dispatch_id, trig.name, key, kind, len(subs), delivered, payload,
                               project=uid, practice=True)
            setup.update({"practice_run": dispatch_id, "practice_at": now_utc().isoformat(),
                          "practice_kind": "own", "practice_finding": None})
            store.set_project_setup(uid, setup)
            return {"dispatch_id": dispatch_id, "delivered": delivered, "subscribers": len(subs)}
        # the project's agents as this project wires them
        agents = {a["name"]: a for a in store.list_catalog_agents(uid)
                  if uid in store.projects_using("agent", a["name"])}
        first = next((a for a in plan.get("agents") or []
                      if a.get("enabled", True) and a.get("on_trigger", True)
                      and a.get("name") in agents), None)
        if first is None:
            _err(ValueError("no agent of this project looks first, so there is nothing to "
                            "practice"), 409)
        trig = next((t for t in triggers if t.name == agents[first["name"]]["trigger"]), None)
        if trig is None:
            _err(ValueError(f"{first['name']} has no trigger to practice with"), 409)
        key, payload = await _practice_input(uid, setup, trig)
        rid = dispatcher.agents.run_now(first["name"], trig.name, key, payload,
                                        woken_by="practice", practice=True, project=uid)
        if rid is None:
            _err(ValueError(f"{first['name']} is already working on {key}; try again when it "
                            "is done"), 409)
        setup.update({"practice_run": rid, "practice_at": now_utc().isoformat(),
                      "practice_kind": "tares"})
        store.set_project_setup(uid, setup)
        return {"run_id": rid}

    def _flag_practice_finding(uid: str, run_id: str, ident: dict | None) -> None:
        """An outside agent's finding recorded within ten minutes of a practice firing, by a
        key of the project (the one setup made, or one made in its place), answers that
        firing: it is practice."""
        setup = store.get_project_setup(uid)
        if not setup or setup.get("practice_kind") != "own" or setup.get("practice_finding"):
            return
        try:
            at = datetime.fromisoformat(str(setup.get("practice_at")))
        except ValueError:
            return
        if (now_utc() - at).total_seconds() > setup_flow.PRACTICE_WINDOW_S:
            return
        if ident is not None and ident.get("project") != uid \
                and ident.get("id") != f"key:{setup.get('own_key_id')}":
            return
        store.set_run_practice(run_id)
        setup["practice_finding"] = run_id
        store.set_project_setup(uid, setup)

    def _record_tool_test(name: str, ok: bool, n_tools: int, error: str | None) -> None:
        """A tool's last test, kept on the guided setup of each project that uses it."""
        for uid in store.projects_using("mcp_server", name):
            setup = store.get_project_setup(uid)
            if setup is None:
                continue
            setup.setdefault("tool_tests", {})[name] = {"ok": ok, "tools": n_tools,
                                                        "error": error, "at": now_utc().isoformat()}
            store.set_project_setup(uid, setup)

    # ── a project's skills (TR-332): instructions its agents load by name ─────
    def _skill_project(uid: str) -> dict:
        p = store.get_project(uid)
        if p is None:
            _err(KeyError(f"unknown project {uid!r}"), 404)
        return p

    def _skill_or_404(uid: str, name: str) -> dict:
        sk = store.get_skill(uid, name)
        if sk is None:
            _err(KeyError(f"project has no skill named {name!r}"), 404)
        return {k: sk[k] for k in ("name", "description", "body", "updated_at")}

    @app.get("/api/projects/{uid}/skills")
    async def list_skills(uid: str):
        """The project's skills, with the agents of the project that loaded each one in the
        last 7 days (`loaded_by`)."""
        _skill_project(uid)
        agents = [a["name"] for a in store.list_catalog_agents()
                  if uid in store.projects_using("agent", a["name"])]
        loads = store.skill_loads(agents, days=7)
        return [{**sk, "loaded_by": loads.get(sk["name"], [])} for sk in store.list_skills(uid)]

    @app.get("/api/projects/{uid}/skills/{name}")
    async def get_skill(uid: str, name: str):
        _skill_project(uid)
        return _skill_or_404(uid, name)

    @app.post("/api/projects/{uid}/skills", status_code=201)
    async def create_skill(uid: str, body: dict = Body(...)):
        _skill_project(uid)
        try:
            name, description, text = skills_mod.validate(
                body.get("name"), body.get("description"), body.get("body"))
        except skills_mod.SkillError as e:
            _err(e)
        if store.get_skill(uid, name) is not None:
            _err(ValueError(f"this project already has a skill named {name!r}; edit it "
                            "instead"), 409)
        if store.get_skill(None, name) is not None:
            # skills are shared (P-TR-216): one by that name is already on Tares
            _err(ValueError(f"a skill named {name!r} is already on Tares; use it, or give "
                            "this one another name"), 409)
        store.upsert_skill(uid, name, description, text)
        return _skill_or_404(uid, name)

    @app.post("/api/projects/{uid}/skills/{name}/use")
    async def use_skill(uid: str, name: str):
        """The project uses a skill already on Tares (shared: an edit shows in every project
        that uses it)."""
        _skill_project(uid)
        if not store.use_skill(uid, name):
            _err(KeyError(f"there is no skill named {name!r} on Tares"), 404)
        return _skill_or_404(uid, name)

    @app.get("/api/skills")
    async def list_cell_skills():
        """Every skill on Tares, with the projects that use it (no bodies)."""
        names = {p["id"]: p["name"] for p in store.list_projects()}
        return [{"name": sk["name"], "description": sk["description"],
                 "used_by": [{"id": p, "name": names.get(p, p)} for p in sk["projects"]]}
                for sk in store.list_all_skills()]

    @app.put("/api/projects/{uid}/skills/{name}")
    async def update_skill(uid: str, name: str, body: dict = Body(...)):
        """Change the description, the body, or both; a field left out keeps its value."""
        _skill_project(uid)
        cur = _skill_or_404(uid, name)
        try:
            _, description, text = skills_mod.validate(
                name, body.get("description", cur["description"]), body.get("body", cur["body"]))
        except skills_mod.SkillError as e:
            _err(e)
        store.upsert_skill(uid, name, description, text)
        store.mark_skill_customized(uid, name)
        return _skill_or_404(uid, name)

    @app.delete("/api/projects/{uid}/skills/{name}")
    async def delete_skill(uid: str, name: str):
        """The project stops using the skill; it is deleted when no other project uses it."""
        _skill_project(uid)
        others = [p for p in store.skill_users(name) if p != uid]
        if not store.delete_skill(uid, name):
            _err(KeyError(f"project has no skill named {name!r}"), 404)
        return {"ok": True, "deleted": name if not others else None, "removed": name,
                "kept_for": others}

    @app.post("/api/projects/{uid}/skills/upload")
    async def upload_skill(uid: str, request: Request):
        """The raw body is a SKILL.md: front matter with name and description, then the body.
        Creates the skill, or replaces the one of the same name."""
        _skill_project(uid)
        raw = await request.body()
        try:
            name, description, text = skills_mod.parse_skill_md(raw.decode("utf-8"))
        except UnicodeDecodeError:
            _err(ValueError("a SKILL.md must be UTF-8 text"))
        except skills_mod.SkillError as e:
            _err(e)
        created = store.upsert_skill(uid, name, description, text)
        if not created:
            store.mark_skill_customized(uid, name)
        return {**_skill_or_404(uid, name), "created": created}

    # ── a project's docs and tickets (TR-403): what a spec session leaves for the session that
    # builds it. Docs belong to the cell and projects include them; tickets are owned by Linear
    # when the project uses it (written by the sync only), by Tares otherwise ────────────────
    def _by(request: Request, body: dict | None = None) -> str:
        """Who made a change: what the caller says (`by`), else its credential's name."""
        said = str((body or {}).get("by") or "").strip()[:100]
        cred = getattr(request.state, "credential", None) or {}
        return said or cred.get("name") or ""

    # The cell's global docs (its AGENTS.md and memory): made the first time a project has docs,
    # included in every project that has docs, so every session working on a project reads them.
    _GLOBAL_SETTING = "global_docs"

    def _global_ids(create: bool = False) -> dict[str, str]:
        """{kind: doc id} of the global docs that exist; with `create`, make the missing ones."""
        try:
            ids = json.loads(store.get_setting(_GLOBAL_SETTING) or "{}")
        except ValueError:
            ids = {}
        ids = {k: v for k, v in ids.items() if store.get_doc(None, v) is not None}
        if create and set(docs_mod.GLOBAL_DOCS) - set(ids):
            for kind, (title, body) in docs_mod.GLOBAL_DOCS.items():
                if kind not in ids:
                    ids[kind] = store.create_doc(None, kind, title, body, by="tares")
            store.set_setting(_GLOBAL_SETTING, json.dumps(ids))
        return ids

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
    async def list_project_docs(uid: str, kind: str = ""):
        """The docs the project includes, without bodies, in reading order (starting prompt,
        spec, plan, AGENTS.md, memory, notes, working docs). The cell's global AGENTS.md and
        memory are among them (`global`) once the project has any doc."""
        _project_or_404(uid)
        ids = _ensure_globals(uid)
        return [_marked(d, ids)
                for d in docs_mod.sort_docs(store.list_docs(uid, kind.strip().lower() or None))]

    @app.get("/api/docs/global")
    async def get_global_docs():
        """The cell's global AGENTS.md and memory, with their bodies; null until the first
        project has docs."""
        ids = _global_ids()
        return {k: (_marked(store.get_doc(None, ids[k]), ids) if k in ids else None)
                for k in docs_mod.GLOBAL_DOCS}

    @app.post("/api/docs/global/{kind}/lines")
    async def add_global_line(kind: str, request: Request, body: dict = Body(...)):
        """{text}: add one line to the global AGENTS.md (`agents`) or memory (`memory`). A line
        already there is not added twice (`added`: false)."""
        if kind not in docs_mod.GLOBAL_DOCS:
            _err(KeyError(f"no global doc {kind!r}; one of: " + ", ".join(docs_mod.GLOBAL_DOCS)),
                 404)
        ids = _global_ids(create=True)
        cur = store.get_doc(None, ids[kind])
        try:
            text, added = docs_mod.append_line(cur["body"], body.get("text"))
        except docs_mod.DocError as e:
            _err(e)
        if added:
            store.update_doc(ids[kind], body=text, by=_by(request, body))
        return {**_marked(store.get_doc(None, ids[kind]), ids), "added": added}

    @app.post("/api/projects/{uid}/docs", status_code=201)
    async def create_project_doc(uid: str, request: Request, body: dict = Body(...)):
        """{kind, title, body}: a new doc in the project."""
        _project_or_404(uid)
        try:
            kind, title, text = docs_mod.validate_doc(body.get("kind"), body.get("title"),
                                                      body.get("body"))
        except docs_mod.DocError as e:
            _err(e)
        doc_id = store.create_doc(uid, kind, title, text, by=_by(request, body))
        _ensure_globals(uid)
        return _doc_or_404(uid, doc_id)

    @app.get("/api/projects/{uid}/docs/{doc_id}")
    async def get_project_doc(uid: str, doc_id: str):
        _project_or_404(uid)
        return _doc_or_404(uid, doc_id)

    @app.put("/api/projects/{uid}/docs/{doc_id}")
    async def update_project_doc(uid: str, doc_id: str, request: Request,
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
        store.update_doc(doc_id, kind=kind, title=title, body=text, by=_by(request, body))
        return _doc_or_404(uid, doc_id)

    @app.delete("/api/projects/{uid}/docs/{doc_id}")
    async def delete_project_doc(uid: str, doc_id: str):
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
    async def use_project_doc(uid: str, doc_id: str):
        """The project includes a doc already on the cell (shared: an edit shows everywhere)."""
        _project_or_404(uid)
        if not store.use_doc(uid, doc_id):
            _err(KeyError(f"there is no doc {doc_id!r} on Tares"), 404)
        return _doc_or_404(uid, doc_id)

    @app.get("/api/docs")
    async def list_cell_docs():
        """Every doc on Tares, with the projects that include it (no bodies)."""
        names = {p["id"]: p["name"] for p in store.list_projects()}
        ids = _global_ids()
        return [{**_marked(d, ids),
                 "projects": [{"id": p, "name": names.get(p, p)} for p in d["projects"]]}
                for d in docs_mod.sort_docs(store.list_cell_docs())]

    def _ticket_out(uid: str, t: dict, with_doc: bool = False) -> dict:
        out = {k: t[k] for k in ("id", "owner", "identifier", "url", "title", "status",
                                 "position", "working_doc", "updated_at")}
        doc = store.get_doc(uid, t["working_doc"]) if t["working_doc"] else None
        out["working_doc_title"] = doc["title"] if doc else None
        if with_doc:
            out["working_doc_body"] = doc["body"] if doc else None
        return out

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
        sync's time and error)."""
        _project_or_404(uid)
        return {"tickets": [_ticket_out(uid, t) for t in store.list_tickets(uid)],
                "linear": store.get_project_linear(uid)}

    @app.post("/api/projects/{uid}/tickets", status_code=201)
    async def create_project_ticket(uid: str, body: dict = Body(...)):
        """{title, status?, position?, working_doc?}: a ticket Tares owns. Refused when the
        project's tickets live in Linear."""
        _project_or_404(uid)
        if store.get_project_linear(uid):
            _err(ValueError("this project's tickets live in Linear; add the ticket there and "
                            "it shows up here on the next sync"), 409)
        try:
            title = docs_mod.title_ok(body.get("title"))
            status = docs_mod.status_ok(body.get("status"))
        except docs_mod.DocError as e:
            _err(e)
        pos = body.get("position")
        tid = store.create_ticket(uid, "tares", title, status,
                                  float(pos) if pos is not None else None,
                                  working_doc=_working_doc_ok(uid, body.get("working_doc")))
        return _ticket_out(uid, _ticket_or_404(uid, tid))

    @app.get("/api/projects/{uid}/tickets/{ref}")
    async def get_project_ticket(uid: str, ref: str):
        """One ticket by its id or Linear identifier, with its working doc's body."""
        _project_or_404(uid)
        return _ticket_out(uid, _ticket_or_404(uid, ref), with_doc=True)

    @app.put("/api/projects/{uid}/tickets/{ref}")
    async def update_project_ticket(uid: str, ref: str, body: dict = Body(...)):
        """Change a ticket. A Linear ticket only takes `working_doc` (its title, status and order
        change in Linear); a Tares ticket also takes title, status and position."""
        _project_or_404(uid)
        t = _ticket_or_404(uid, ref)
        fields: dict = {}
        if "working_doc" in body:
            fields["working_doc"] = _working_doc_ok(uid, body.get("working_doc"))
        own = {"title", "status", "position"} & set(body)
        if own and t["owner"] == "linear":
            _err(ValueError(f"{t['identifier'] or t['id']} lives in Linear: change its "
                            + ", ".join(sorted(own)) + " there"), 409)
        try:
            if "title" in body:
                fields["title"] = docs_mod.title_ok(body["title"])
            if "status" in body:
                fields["status"] = docs_mod.status_ok(body["status"])
        except docs_mod.DocError as e:
            _err(e)
        if "position" in body and body["position"] is not None:
            fields["position"] = float(body["position"])
        if fields:
            store.update_ticket(t["id"], **fields)
        return _ticket_out(uid, _ticket_or_404(uid, t["id"]), with_doc=True)

    @app.delete("/api/projects/{uid}/tickets/{ref}")
    async def delete_project_ticket(uid: str, ref: str):
        """Delete a Tares ticket (its working doc stays). A Linear ticket is removed in Linear."""
        _project_or_404(uid)
        t = _ticket_or_404(uid, ref)
        if t["owner"] == "linear":
            _err(ValueError(f"{t['identifier'] or t['id']} lives in Linear: remove it there"),
                 409)
        store.delete_ticket(t["id"])
        return {"ok": True, "deleted": t["id"]}

    # ── the Claude Code sessions that worked in a project (TR-405) ────────────
    @app.get("/api/projects/{uid}/sessions")
    async def list_project_sessions(uid: str):
        """The sessions that worked in the project (the spec session among them), newest activity
        first."""
        _project_or_404(uid)
        return {"sessions": store.project_sessions(uid)}

    @app.get("/api/projects/{uid}/sessions/{sid}")
    async def get_project_session(uid: str, sid: str, limit: int = 200, offset: int = 0):
        """One session's lines, oldest first, the ones from before it named the project
        included."""
        _project_or_404(uid)
        if not any(s["session"] == sid for s in store.project_sessions(uid)):
            _err(KeyError(f"session {sid!r} did not work in this project"), 404)
        src = next((n for n, c in runtime.catalog.sources.items()
                    if c.connector == "claude_code"), "claude_code")
        return {"session": sid,
                "lines": store.key_events(src, sid, limit=min(max(limit, 1), 1000),
                                          offset=max(offset, 0))}

    # ── Linear (TR-408): the cell's connection, and the Linear project a project's tickets live
    # in. Tares only reads from Linear; sessions write with Linear's own tools ───────────────
    def _linear_err(e: Exception):
        _err(e, 502 if "reach" in str(e) or "answered" in str(e) else 400)

    @app.get("/api/linear")
    async def linear_status():
        """Whether Linear is connected (never the token), as whom, and whether sign-in with
        Linear is set up (an OAuth application's client id)."""
        return _linear.public_connection(store)

    @app.post("/api/linear")
    async def linear_connect_key(body: dict = Body(...)):
        """{api_key}: connect with a personal API key, checked against Linear first."""
        try:
            await _linear.connect_api_key(store, str(body.get("api_key") or ""))
        except _linear.LinearError as e:
            _linear_err(e)
        return _linear.public_connection(store)

    @app.put("/api/linear/oauth-app")
    async def linear_oauth_app(body: dict = Body(...)):
        """{client_id, client_secret?}: the Linear OAuth application sign-in uses. A secret left
        out keeps the saved one; with PKCE it is optional."""
        secret = body.get("client_secret")
        _linear.save_oauth_app(store, str(body.get("client_id") or ""),
                               None if secret is None else str(secret))
        return _linear.public_connection(store)

    def _linear_redirect(request: Request) -> str:
        from .setup_flow import public_base
        return public_base(str(request.base_url)).rstrip("/") + LINEAR_CALLBACK

    @app.get("/api/linear/oauth/start")
    async def linear_oauth_start(request: Request):
        """The Linear page to send the browser to, and the callback URL the OAuth application
        must list."""
        redirect = _linear_redirect(request)
        try:
            url = _linear.oauth_start(store, redirect, _gh_app.sign_state)
        except _linear.LinearError as e:
            _linear_err(e)
        return {"url": url, "redirect_uri": redirect}

    @app.get(LINEAR_CALLBACK)
    async def linear_oauth_callback(code: str = "", state: str = "", error: str = ""):
        from urllib.parse import urlencode
        params = {"tab": "linear"}
        try:
            if error:
                raise _linear.LinearError(f"Linear sign-in was not completed: {error}")
            await _linear.oauth_finish(store, code, state, _gh_app.verify_state)
            params["event"] = "connected"
        except (ValueError, _linear.LinearError) as e:
            params["error"] = str(e)
        return RedirectResponse(f"/settings?{urlencode(params)}", status_code=303)

    @app.delete("/api/linear")
    async def linear_disconnect():
        """Forget the connection (an OAuth token is revoked at Linear). Linked projects keep
        their tickets and stop syncing until Linear is connected again."""
        conn = _linear.disconnect(store)
        if conn:
            await _linear.revoke(conn)
        return _linear.public_connection(store)

    @app.get("/api/linear/projects")
    async def linear_projects(q: str = ""):
        """Linear projects this connection can see, most recently updated first, for a picker."""
        try:
            return {"projects": await _linear.list_projects(store, q)}
        except _linear.LinearError as e:
            _linear_err(e)

    async def _linear_sync_now(uid: str) -> None:
        envelopes = await _linear.sync_project(store, uid)
        if envelopes:
            store.append(envelopes)

    @app.post("/api/projects/{uid}/linear")
    async def link_linear_project(uid: str, body: dict = Body(...)):
        """{project}: the Linear project (id, URL or name) this project's tickets live in. Runs a
        first sync; from then on the `linear` source keeps them in sync. A project that already
        has Tares tickets cannot be linked (they would be two lists)."""
        _project_or_404(uid)
        if any(t["owner"] == "tares" for t in store.list_tickets(uid)):
            _err(ValueError("this project already has its own tickets in Tares; a project's "
                            "tickets live in one place"), 409)
        try:
            found = await _linear.find_project(store, str(body.get("project") or ""))
        except _linear.LinearError as e:
            _linear_err(e)
        cur = store.get_project_linear(uid)
        if cur and cur.get("id") != found["id"]:
            _err(ValueError(f"this project is linked to Linear project {cur.get('name')!r}; "
                            "unlink it first"), 409)
        store.set_project_linear(uid, {**(cur or {}), **found})
        if _linear.ensure_source(store):
            runtime.reload_catalog()
        store.upsert_project_object(uid, "source", f"+source:{_linear.SOURCE_NAME}",
                                    _linear.SOURCE_NAME)
        try:
            await _linear_sync_now(uid)
        except _linear.LinearError:
            pass   # recorded on the link; the answer shows it
        return {"tickets": [_ticket_out(uid, t) for t in store.list_tickets(uid)],
                "linear": store.get_project_linear(uid)}

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
        return {"tickets": [_ticket_out(uid, t) for t in store.list_tickets(uid)],
                "linear": store.get_project_linear(uid)}

    @app.delete("/api/projects/{uid}/linear")
    async def unlink_linear_project(uid: str):
        """The project stops using Linear. Its tickets stay, now owned by Tares, with their
        working docs."""
        _project_or_404(uid)
        store.set_project_linear(uid, None)
        for t in store.list_tickets(uid):
            if t["owner"] == "linear":
                store.update_ticket_owner(t["id"], "tares")
        return {"tickets": [_ticket_out(uid, t) for t in store.list_tickets(uid)],
                "linear": None}

    # ── joining a project (TR-335, TR-336): its keys, an external agent's subscription to it,
    # the findings recorded in it, and stats over its sources ────────────────────────────────
    def _project_or_404(uid: str) -> dict:
        p = store.get_project(uid)
        if p is None:
            _err(KeyError(f"unknown project {uid!r}"), 404)
        return p

    @app.get("/api/projects/{uid}/keys")
    async def list_project_keys(uid: str):
        """The project's active keys, without their secrets."""
        _project_or_404(uid)
        return {"keys": [k for k in store.list_api_keys(project=uid) if not k["revoked_at"]],
                "enforced": bool(AUTH_TOKEN)}

    @app.post("/api/projects/{uid}/keys", status_code=201)
    async def create_project_key(uid: str, body: dict = Body(...)):
        """{name}: a key that reads this project only and records findings in it. The secret is
        in this response only."""
        _project_or_404(uid)
        name = str(body.get("name") or "").strip()
        if not name:
            _err(ValueError("name is required"))
        if len(name) > 64:
            _err(ValueError("name is at most 64 characters"))
        return _make_key(name, sorted(_PROJECT_SCOPES), uid)

    def _valid_hook_url(url: str) -> str:
        url = (url or "").strip()
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.netloc:
            _err(ValueError("url must be an http or https URL your agent listens on"))
        return url

    async def _refuse_internal_hook(url: str) -> None:
        """A project key is handed to an outside agent, so its webhook may not point inside the
        cell's network (loopback, private ranges, link-local such as cloud metadata).
        TARES_WEBHOOK_ALLOW_PRIVATE=1 allows it, for a cell whose agents live on its network."""
        if os.environ.get("TARES_WEBHOOK_ALLOW_PRIVATE", "").lower() in ("1", "true", "yes"):
            return
        host = urlsplit(url).hostname or ""
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except OSError:
            _err(ValueError(f"cannot resolve {host!r}"), 400)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                    or ip.is_multicast or ip.is_unspecified):
                _err(ValueError(f"a project key cannot send firings to {host} (an internal "
                                "address); use a public URL"), 400)

    @app.post("/api/projects/{uid}/subscribe")
    async def subscribe_project(uid: str, request: Request, body: dict = Body(...)):
        """{url}: POST every firing of every trigger of the project to `url`, the triggers added
        later included. The body is the usual firing plus `project`. Subscribing the same URL
        again with the same key returns the subscription it already has."""
        _project_or_404(uid)
        url = _valid_hook_url(str(body.get("url") or ""))
        if getattr(request.state, "project_key", None):
            await _refuse_internal_hook(url)
        ident = getattr(request.state, "credential", None)
        created_by = ident["id"] if ident else None
        for s in store.list_project_subscriptions(uid):
            if s["url"] == url and s["created_by"] == created_by:
                return {"subscription_id": s["subscription_id"], "project": uid, "existing": True}
        sid = "sub_" + uuid.uuid4().hex[:8]
        # created_by: revoking the key removes the subscription with it
        store.add_subscription(sid, "", url, created_by=created_by, project=uid)
        return {"subscription_id": sid, "project": uid}

    @app.delete("/api/projects/{uid}/subscribe/{sid}")
    async def unsubscribe_project(uid: str, sid: str, request: Request):
        """A project key removes only its own subscriptions; an admin any of the project's."""
        _project_or_404(uid)
        sub = store.get_subscription(sid)
        if sub is None or sub["project"] != uid:
            _err(KeyError(f"project has no subscription {sid!r}"), 404)
        pk = _pk(request)
        if pk:
            ident = getattr(request.state, "credential", None) or {}
            if sub["created_by"] != ident.get("id"):
                _err(PermissionError("that subscription belongs to another key"), 403)
        store.remove_subscription(sid)
        return {"ok": True}

    @app.get("/api/projects/{uid}/external-agents")
    async def project_external_agents(uid: str):
        """The agents that joined the project with a subscription: where it delivers (masked,
        a URL can carry the receiver's secret), which key made it, and its last delivery."""
        _project_or_404(uid)
        keys = {f"key:{k['id']}": k for k in store.list_api_keys()}
        out = []
        for s in store.list_project_subscriptions(uid):
            key = keys.get(s["created_by"] or "")
            name, masked = _agent_identity(s["url"])
            out.append({"subscription_id": s["subscription_id"], "name": name, "url": masked,
                        "key_id": key["id"] if key else None,
                        "key_name": (key["name"] if key else
                                     "auth token" if s["created_by"] == "env:auth" else None),
                        "created_at": s["created_at"],
                        "last_delivery": store.last_delivery(s["subscription_id"])})
        return {"agents": out}

    def _finding_row(r: dict) -> dict:
        return {"run_id": r["id"], "agent": r["agent"], "entity": r["key"],
                "verdict": r.get("verdict"), "finding": r.get("finding"),
                "at": r["started_at"], "trigger": r.get("trigger") or None,
                "external": r.get("woken_by") == "external"}

    @app.get("/api/projects/{uid}/findings")
    async def project_findings(uid: str, entity: str = "", agent: str = "", limit: int = 20):
        """Findings recorded in the project, newest first: its Tares agents' and the external
        agents'. `entity` and `agent` narrow them."""
        _project_or_404(uid)
        rows = store.project_findings(uid, entity=entity.strip(), agent=agent.strip(),
                                      limit=max(1, min(int(limit), 200)))
        return {"findings": [_finding_row(r) for r in rows]}

    _VERDICT = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
    _LABEL = re.compile(r"^[A-Za-z0-9_]{1,64}$")

    @app.post("/api/projects/{uid}/findings", status_code=201)
    async def record_project_finding(uid: str, request: Request, body: dict = Body(...)):
        """{entity, finding, verdict?, label?, headline?, next_step?}: an external agent records what it concluded about
        an entity. It is stored like a Tares agent's finding (on the entity's timeline, read by
        findings triggers) and shows in the project timeline as a run marked external. The agent
        is the key's name; `label` is the label the entity is a value of (default: the entity
        label of the project's first trigger)."""
        _project_or_404(uid)
        entity = str(body.get("entity") or "").strip()
        finding = str(body.get("finding") or "").strip()
        if not entity or len(entity) > 512:
            _err(ValueError("entity is required (at most 512 characters): what the finding is about"))
        if not finding:
            _err(ValueError("finding is required"))
        if len(finding.encode()) > 64 * 1024:
            _err(ValueError("a finding is at most 64 KB"))
        verdict = str(body.get("verdict") or "").strip().lower() or None
        if verdict and not _VERDICT.match(verdict):
            _err(ValueError("verdict is one lowercase word, e.g. rca or resolved"))
        headline = " ".join(str(body.get("headline") or "").split()) or None
        next_step = " ".join(str(body.get("next_step") or "").split()) or None
        if headline and len(headline) > 100:
            _err(ValueError("headline is one line of at most 100 characters"))
        if next_step and len(next_step) > 300:
            _err(ValueError("next_step is at most 300 characters"))
        label = str(body.get("label") or "").strip() or None
        if label and not _LABEL.match(label):
            _err(ValueError("label is a label name: letters, digits and underscores"))
        if label is None:
            from .config import trigger_entity_label
            trig = next((t for t in sorted(runtime.catalog.triggers, key=lambda t: t.name)
                         if uid in store.projects_using("trigger", t.name)), None)
            label = trigger_entity_label(trig, runtime.catalog.sources) if trig else None
        ident = getattr(request.state, "credential", None)
        if ident:   # the key's name (a project key's always)
            agent = ident["name"]
        else:   # an open instance: whoever calls names itself, or is "external agent"
            agent = str(body.get("agent") or "").strip()[:64] or "external agent"
        if store.get_catalog_agent(agent) is not None:
            _err(ValueError(f"{agent!r} is also the name of a Tares agent; record the finding with "
                            "a key of another name"), 409)
        run_id = await dispatcher.agents.record_external(uid, agent, entity, finding,
                                                         verdict=verdict, label=label,
                                                         headline=headline, next_step=next_step)
        _flag_practice_finding(uid, run_id, ident)
        return {"ok": True, "run_id": run_id, "agent": agent, "entity": entity,
                "verdict": verdict}

    @app.post("/api/projects/{uid}/stats")
    async def project_stats(uid: str, body: dict = Body(...)):
        """{by, window?, where?, top?, sources?}: counts per value of the label `by` over the
        project's sources, the last window against the one before, as a few lines of text."""
        from .stats import stats_table
        _project_or_404(uid)
        by = str(body.get("by") or "").strip()
        if not by:
            _err(ValueError('stats needs `by`, the label to count per, e.g. "service"'))
        names, scope = _project_view(uid)
        named = body.get("sources") or []
        if named:
            if not isinstance(named, list) or set(named) - set(names):
                _err(PermissionError("sources must be sources of this project"), 403)
            names = [s for s in names if s in named]
        where = body.get("where") or None
        if where is not None and not isinstance(where, dict):
            _err(ValueError("where is a {label: value} object"))
        window = str(body.get("window") or "30m")
        p = store.get_project(uid) or {}
        try:
            text = stats_table(store, names, by, window, where=where, top=body.get("top") or 20,
                               scope=f"project {p.get('name') or uid}", project_rows=scope)
        except ValueError as e:
            _err(e)
        store.log_query("s_" + uuid.uuid4().hex[:12], "(stats)", f"by {by}", window, 0, "http")
        return {"stats": text}

    # /api/usecases*: the pre-1.14 routes, same handlers, old response shape ({"usecases"},
    # {"recipes"}, and `recipe` on an instance, which get() still emits). Not in the schema;
    # removed two releases after 1.14.
    async def legacy_recipes():
        return {"recipes": projects.list_templates()}

    async def legacy_list():
        return {"usecases": projects.list()}

    for _path, _fn, _methods, _status in (
            ("/api/usecases/recipes", legacy_recipes, ["GET"], 200),
            ("/api/usecases", legacy_list, ["GET"], 200),
            ("/api/usecases", create_project, ["POST"], 201),
            ("/api/usecases/{uid}", get_project, ["GET"], 200),
            ("/api/usecases/{uid}", update_project, ["PUT"], 200),
            ("/api/usecases/{uid}", delete_project, ["DELETE"], 200),
            ("/api/usecases/{uid}/pause", pause_project, ["POST"], 200),
            ("/api/usecases/{uid}/resume", resume_project, ["POST"], 200),
            ("/api/usecases/{uid}/repair", repair_project, ["POST"], 200),
            ("/api/usecases/recipes/{key}/detect", project_detect, ["POST"], 200),
            ("/api/usecases/{uid}/actions/{name}", project_action, ["POST"], 200),
            ("/api/usecases/{uid}/summary", project_summary, ["GET"], 200)):
        app.add_api_route(_path, _fn, methods=_methods, status_code=_status, include_in_schema=False)

    # ── management API: catalog import/export ────────────────────────────────
    @app.get("/api/catalog/export")
    async def catalog_export(sources: str | None = None, include_secrets: bool = False):
        """Catalog YAML. Defaults (no params, as the agent/MCP call it): all sources, secrets
        OMITTED. `sources=a,b` limits to a subset (triggers and agents filtered to stay consistent);
        `include_secrets=true` emits real connector secrets (admin-gated route)."""
        src = [s for s in sources.split(",") if s] if sources else None
        return PlainTextResponse(export_db_to_yaml(store, src, include_secrets),
                                 media_type="application/yaml")

    @app.post("/api/catalog/import")
    async def catalog_import(body: ImportReq):
        if body.mode == "replace":
            store.clear_catalog()
        try:
            counts = import_yaml_to_db(store, body.yaml, engine=projects)
            store.migrate_claude_code_repo_label()
        except (CatalogError, ProjectError) as e:
            _err(e)
        except Exception as e:
            _err(ValueError(f"invalid YAML: {e}"))
        runtime.reload_catalog()
        return {"ok": True, **counts}

    # ── metering ──────────────────────────────────────────────────────────────
    @app.get("/api/usage")
    async def usage():
        """What this instance is using: db + WAL bytes, the volume they sit on, and event counts.
        `max_bytes` is the operator's TARES_MAX_DB_SIZE, else the volume the database sits on
        (`max_bytes_source` says which); null only when neither is known. At INGEST_PAUSE_PCT of it
        ingest is refused and polls pause (`ingest_paused`), so the disk never fills.
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
    async def usage_model(days: int = 30):
        """The cell's Anthropic spend meter: all-time totals plus a per-day tail, split by surface
        (agent runs vs Ask). Generic data only — a hosted control plane polls this to enforce
        credits, a self-hoster reads their own bill coming; the cell attaches no meaning to it.
        `cost_usd` is a floor: `uncosted_calls` counts rows whose model had no known price, and
        runs from before metering existed are absent entirely."""
        return store.model_usage_summary(days=max(1, min(int(days), 366)))

    # ── console UI (built SPA; catch-all registered last so API routes win) ──
    @app.get("/{path:path}", include_in_schema=False)
    async def ui(path: str):
        # An unmatched /api/* or /ingest/* path must never answer with the SPA's HTML and a 200 —
        # a client checking the status reads that as success and fails to parse (TR-226). The
        # console never requests these prefixes, so nothing legitimate is lost.
        if path == "api" or path.startswith(("api/", "ingest/")):
            _err(KeyError(f"unknown API path /{path}"), 404)
        return _serve_ui(path)

    return app
