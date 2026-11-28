"""taresd: the always-on daemon. Owns the store; takes the session lines and GitHub App webhooks
in; serves the local HTTP API (the factory's routes and the management API) and the built-in
console UI. There is no source catalog (Tares 2.0, TR-1811).

`make_app()` builds the app from routers: one per area under tares/routes/, each given the one
`Deps` (tares/deps.py), then the factory modules that register their own routes, the ee team, and
the console's catch-all last. No route is defined here (TR-1814).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
import traceback
from contextlib import asynccontextmanager
from functools import partial

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import claude_usage, factory_api, guardrails_api, guard, integrations_api, licence
from . import loop as loop_mod
from . import metrics as _metrics
from . import morning_status as morning_mod
from . import project_secrets
from . import providers as providers_mod
from . import report as report_mod
from . import session_archive
from . import standup as standup_mod
from . import team as team_mod
from . import waiting_slack as waiting_slack_mod
from .deps import Deps
from .factory_projects import Projects
from .integrations import hooks as integrations_hooks
from .routes import auth as auth_routes
from .routes import console as console_routes
from .routes import docs as docs_routes
from .routes import health as health_routes
from .routes import home as home_routes
from .routes import milestones as milestones_routes
from .routes import projects as projects_routes
from .routes import sessions as sessions_routes
from .routes import settings as settings_routes
from .routes import slack as slack_routes
from .routes import standup as standup_routes
from .routes import tickets as tickets_routes
from .screens import factory_crew as crew_screen
from .screens import factory_docs as docs_screen
from .screens import factory_merge_queue as merge_queue_screen
from .screens import factory_people as people_screen
from .screens import factory_project as project_screen
from .screens import factory_projects as projects_screen
from .screens import factory_reviews as reviews_screen
from .screens import factory_settings as settings_screen
from .screens import factory_setup as setup_screen
from .screens import factory_ticket as ticket_screen
from .screens import factory_tickets as tickets_screen
from .screens import start as start_screen
from .slack import SETTING_KEY as SLACK_TOKEN_SETTING  # noqa: F401  (ee/tests read it here)
from .slack import resolve_token as resolve_slack_token
from .store import Store, StoreUnavailable


# The data directory: the session store's `tares.sqlite` (without TARES_STATE_DSN) and the default
# archive are in it. `tares up` sets it from --data-dir; taresd alone uses the current directory.
DATA_DIR = os.path.expanduser(os.getenv("TARES_HOME") or ".")

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


def storage_limit(data_dir: str) -> tuple[int | None, str]:
    """(max_bytes, where it comes from): TARES_MAX_DB_SIZE when set ("env"), else the total size
    of the volume the data directory sits on ("volume"). None only when neither is known."""
    if MAX_DB_SIZE:
        return MAX_DB_SIZE, "env"
    try:
        return shutil.disk_usage(os.path.abspath(data_dir)).total, "volume"
    except OSError:
        return None, ""


def storage_state(store, data_dir: str) -> dict:
    """How full this instance is against its limit, and whether ingest is paused because of it:
    the session store (the SQLite file and its WAL, or the Postgres database) plus the archive
    (`Store.disk_bytes`, measured at most every few seconds, so every probe and every ingest can
    afford it)."""
    limit, origin = storage_limit(data_dir)
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
    pass on their own, unlike a corrupt SQLite file or a full disk."""
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
    app.include_router(console_routes.degraded_router(reason, retry, bool(AUTH_TOKEN), LOGIN_URL,
                                                      _serve_ui))
    return app


def _cloud_links() -> dict:
    """The control plane's links the public /health hands the console, each only while its
    variable is set (all public, non-secret: login_url is where the logged-out console sends the
    browser to authenticate). Read from this module at each call."""
    links = {"login_url": LOGIN_URL, "workspace_url": WORKSPACE_URL,
             "github_connect_url": GITHUB_CONNECT_URL, "slack_connect_url": SLACK_CONNECT_URL,
             "linear_connect_url": LINEAR_CONNECT_URL, "workspaces_url": WORKSPACES_URL,
             "workspace_api_url": WORKSPACE_API_URL, "logout_url": LOGOUT_URL}
    return {k: v for k, v in links.items() if v}


# The console screens (tares/screens/), registered in this order after factory_api.
_SCREENS = (
    projects_screen,       # TR-733
    project_screen,        # TR-734
    tickets_screen,        # TR-806
    ticket_screen,         # TR-760
    reviews_screen,        # TR-735
    crew_screen,           # TR-736
    docs_screen,           # TR-737
    settings_screen,       # TR-738
    start_screen,          # TR-756
    people_screen,         # TR-1011
    merge_queue_screen,    # TR-1094
    setup_screen,          # TR-871
)


def make_app() -> FastAPI:
    try:
        # TARES_STATE_DSN (a cloud cell): everything is in that Postgres database. Unset, it is in
        # tares.sqlite in the data directory.
        store = Store(DATA_DIR, state_dsn=os.getenv("TARES_STATE_DSN") or None)
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

    # What the daemon starts in the background, by name, in the order it starts them: the threads
    # here, then the lifespan's tasks (app.state.started_tasks, tests/test_factory_startup.py).
    started: list[str] = []
    # the hourly usage totals and the ten minute pass over the session store (session_ingest),
    # in the background; none of it reads the session lines at start
    if store.start_session_reconcile() is not None:
        started.append("session_reconcile")

    projects = Projects(store)

    data_dir = DATA_DIR
    deps = Deps(store, projects, auth_token=AUTH_TOKEN, data_dir=data_dir,
                storage=lambda: storage_state(store, data_dir), capabilities=CAPABILITIES,
                version=_installed_version, started_mono=_STARTED_MONO, cloud_links=_cloud_links)

    @asynccontextmanager
    async def lifespan(_app):
        # the daemon's own counters (/metrics, TR-308): storage gauges read on scrape, the event
        # loop watched by a one-second timer for the whole life of the process
        _metrics.set_storage_probe(lambda: {**store.storage_usage(), **{
            k: v for k, v in deps.storage().items()
            if k in ("max_bytes", "pct_used", "paused")}})
        try:
            from importlib.metadata import version as _pkg_version
            _metrics.set_info(_pkg_version("tares"))
        except Exception:
            _metrics.set_info(None)
        _metrics.prime(providers=[p["id"] for p in providers_mod.list_providers(store)["providers"]])
        _metrics.set_licence_probe(licence.peek)
        loop_stop = asyncio.Event()
        loop_mod.attach(asyncio.get_running_loop())   # handlers in threads start work here
        from .integrations import sync as integration_sync
        tasks: dict[str, asyncio.Task] = {}

        def start(name: str, coro) -> None:
            tasks[name] = asyncio.create_task(coro)
            _app.state.started_tasks.append(name)
        _app.state.started_tasks = list(started)
        # providers the environment handed the cell list their models now, not on a click
        start("provider_models", providers_mod.discover_env_entries(store))
        start("event_loop_watch", _metrics.watch_event_loop(loop_stop))
        # pull requests read again in the background, not only when a project is opened (TR-1010)
        start("pr_sync", factory_api.sync_prs(store, loop_stop))
        # the morning status goes out from here, whoever's laptop is shut (TR-825)
        start("morning_status", deps.morning.run(loop_stop))
        # the licence: checked again every minute, and the desk told as it nears its end (TR-912)
        start("licence", licence.run(store, loop_stop))
        start("waiting_slack", deps.waiting_slack.run(loop_stop))
        # sessions idle for a week move from the session store to the archive, once an hour (TR-1506)
        start("session_archive", session_archive.run(store, loop_stop))
        # the weekly figures the report reads, built once and then kept current (TR-1666)
        start("report", report_mod.run(store, loop_stop))
        # Linear and GitHub Issues tickets kept in step by their integrations (TR-1806)
        start("integration_sync", integration_sync.run(store, loop_stop))
        print("taresd: console at / · management API at /api")
        yield
        loop_stop.set()
        for name, task in tasks.items():
            if name != "provider_models":   # a one-off call: left to finish, as before
                task.cancel()

    # the interactive API reference lives under /api so /docs is the console's Docs and memory page
    app = FastAPI(title="taresd", lifespan=lifespan, docs_url="/api/reference", redoc_url=None)
    app.state.store = store   # for in-process tests
    integrations_hooks.bind(store)   # the guardrail hooks resolve a project's guardrails from it
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    # One answer for an action that needs a licence the cell does not have (TR-912). The three
    # keys sit at the top of the body, not under `detail`, so a client can read `code` directly.
    async def _licence_required(_request, e: licence.LicenceRequired):
        if licence.hosted():
            # a Tares Cloud cell: plain words whatever the reason, and no page to read about it
            return JSONResponse({"code": "licence_required", "message": licence.CLOUD_WORDS,
                                 "url": ""}, status_code=402)
        return JSONResponse({"code": "licence_required", "message": e.message,
                             "url": licence.LICENCE_URL}, status_code=402)
    app.add_exception_handler(licence.LicenceRequired, _licence_required)

    licence.current(store, force=True)

    guard.install(app, deps)   # the auth guard (tares/guard.py); none while auth is off

    # per-route timing in /metrics (TR-652): added after every other middleware so it is the
    # outermost one and times auth rejections too
    app.add_middleware(_metrics.RequestTiming)
    app.state.deps = deps

    # ── the routers, in the order FastAPI matches them: a literal path before a parametrised one
    # that would also match it ──
    app.include_router(health_routes.router(deps))
    app.include_router(sessions_routes.router(deps))
    app.include_router(auth_routes.router(deps))
    app.include_router(settings_routes.router(deps))
    app.include_router(slack_routes.router(deps))
    app.include_router(projects_routes.router(deps))
    app.include_router(docs_routes.router(deps))
    app.include_router(tickets_routes.router(deps))
    app.include_router(milestones_routes.router(deps))
    # the crew's standup report: before factory_api, whose /api/crew/{name} would take it
    app.include_router(standup_routes.router(deps))

    # The morning status (TR-825) reuses the standup. Its report leaves private projects out:
    # it runs with no caller and goes to a shared place.
    deps.morning = morning_mod.MorningStatus(
        store,
        report=lambda since, until: standup_mod.facts(
            store, since, until,
            lambda uid, tickets, ms: factory_api.project_progress(store, uid, tickets, ms),
            hide={p["id"] for p in store.private_projects()}),
        summarise=partial(standup_routes.summarise, deps),
        post_slack=partial(slack_routes.post_slack, deps),
        slack_connected=lambda: bool(resolve_slack_token(store)[0]))
    app.state.morning = deps.morning

    # ── the software factory's crew (M3 onward): routes in tares/factory_api.py ──
    deps.fx = factory_api.register(app, deps)
    # a direct message on Slack when something new waits for a person (TR-1458)
    deps.waiting_slack = waiting_slack_mod.WaitingSlack(store, deps.fx.waiting_for_person)
    waiting_slack_mod.register(app, deps)
    app.state.waiting_slack = deps.waiting_slack
    for screen in _SCREENS:
        screen.register(app, deps)
    integrations_api.register(app, deps)
    guardrails_api.register(app, deps)
    project_secrets.register(app, deps)   # TR-775
    app.include_router(home_routes.router(deps))
    # what the Claude Code sessions spend: the plan's limits and tokens by project
    claude_usage.register(app, deps)

    # ── the team seam (TR-914): the order every route registration follows ──
    # FastAPI answers with the first route that matches, so the order is the contract:
    #   1. core routes, factory_api.register and the screens (all above);
    #   2. the ee team's routes, when tares_ee loads, whatever the licence (each ee route answers
    #      licence_required itself while the licence does not allow it);
    #   3. the fallbacks: one route per path that moved to ee, answering licence_required when no
    #      ee team registered it (an ee route registered in 2 wins over its fallback);
    #   4. the console catch-all below, last.
    # A route that moves to ee is deleted from core in the same PR, so no core route shadows it.
    team_mod.register_ee(app, deps)   # never raises: a broken ee team leaves a single-person cell
    team_mod.register_fallbacks(app, deps)

    # the console catch-all, last: every route above wins over it
    app.include_router(console_routes.router(deps, _serve_ui))
    return app
