"""The daemon's own counters, served at `GET /metrics` in the Prometheus text format (TR-308).

What a cell is doing, for a fleet dashboard and alerts: events in, poll errors, source health,
agent runs and their outcomes, model calls, tokens and spend by provider, database size against
its limit, trigger evaluations and how long they take, the event loop's lag, and how long each
HTTP route takes (`tares_http_request_duration_seconds`, TR-652) and where the time of the ticket
brief, the builder brief and merge-ready goes (`tares_route_phase_seconds`, TR-1476). Labels name the source, agent,
provider, route template or status; never an entity key, an ID or anything from a payload.

The endpoint is public unless TARES_METRICS_TOKEN is set; then it wants
`Authorization: Bearer <token>` (TR-653).

Every hook here is a no-op when the client library is missing, so nothing else in the daemon
guards its calls. The endpoint answers 404 in that case.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import time

try:
    from prometheus_client import (CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, REGISTRY,
                                   generate_latest)
    AVAILABLE = True
except ImportError:   # pragma: no cover - the dependency is base; this is belt and braces
    AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain"

if AVAILABLE:
    EVENTS = Counter("tares_events_ingested_total", "Events stored, per source", ["source"])
    POLLS = Counter("tares_polls_total", "Poll attempts, per source and outcome",
                    ["source", "outcome"])   # ok | error | paused
    SOURCE_STATE = Gauge("tares_source_state", "Source health: 1 for the state the source is in",
                         ["source", "state"])   # ok | error | paused | push
    AGENT_RUNS = Counter("tares_agent_runs_total", "Tares agent runs, per agent and outcome",
                         ["agent", "status"])   # ok | empty | exhausted | capped | failed
    AGENT_RUN_SECONDS = Histogram("tares_agent_run_seconds", "Wall time of an agent run", ["agent"],
                                  buckets=(1, 5, 10, 20, 30, 60, 120, 300, 600))
    MODEL_CALLS = Counter("tares_model_calls_total", "Model calls, per provider and surface",
                          ["provider", "surface"])
    MODEL_TOKENS = Counter("tares_model_tokens_total", "Tokens, per provider, surface and direction",
                           ["provider", "surface", "direction"])   # input | output
    MODEL_SPEND = Counter("tares_model_spend_usd_total", "Priced model spend in USD, per provider "
                          "and surface (unpriced calls add nothing; see tares_model_unpriced_calls_total)",
                          ["provider", "surface"])
    MODEL_UNPRICED = Counter("tares_model_unpriced_calls_total", "Model calls on a model with no "
                             "price, per provider", ["provider"])
    TRIGGER_EVALS = Counter("tares_trigger_evaluations_total", "Trigger evaluation passes")
    TRIGGER_FIRINGS = Counter("tares_trigger_firings_total", "Trigger firings, per trigger", ["trigger"])
    TRIGGER_EVAL_SECONDS = Histogram("tares_trigger_evaluation_seconds",
                                     "Wall time of one evaluation pass (runs on the event loop)",
                                     buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
    DB_BYTES = Gauge("tares_db_bytes", "Database plus WAL size in bytes")
    DB_MAX_BYTES = Gauge("tares_db_max_bytes", "The storage limit ingest is measured against (0 when unknown)")
    STORAGE_PCT = Gauge("tares_storage_pct", "Database size as a percentage of the limit (0 to 100; 0 when unknown)")
    INGEST_PAUSED = Gauge("tares_ingest_paused", "1 while ingest is refused for storage")
    EVENTS_STORED = Gauge("tares_events_stored", "Events in the database")
    LOOP_LAG = Gauge("tares_event_loop_lag_seconds", "How late the event loop ran its last one-second timer")
    LOOP_STALLS = Counter("tares_event_loop_stalls_total", "Timer ticks that ran more than half a second late")
    LICENCE_VALID = Gauge("tares_licence_valid", "1 while the cell holds a valid licence key")
    LICENCE_SEATS = Gauge("tares_licence_seats", "Seats the licence covers (0 when there is none)")
    LICENCE_USED = Gauge("tares_licence_used", "People on the cell, counted against the licence's seats")
    LICENCE_DAYS_LEFT = Gauge("tares_licence_days_left", "Days until the licence ends; negative once "
                              "it has ended (0 when there is none)")
    INFO = Gauge("tares_info", "Build information", ["version"])
    HTTP_SECONDS = Histogram("tares_http_request_duration_seconds",
                             "Wall time of an HTTP request until the app returns (the whole body "
                             "for a streaming response), per route template, method and status class",
                             ["route", "method", "status"],
                             buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
    PHASE_SECONDS = Histogram("tares_route_phase_seconds",
                              "Own wall time of one phase of a route (the time of the phases "
                              "inside it left out), so the phases of a route add up to its time",
                              ["route", "phase"],
                              buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60))
    SESSION_INGEST_SECONDS = Histogram("tares_session_ingest_seconds",
                                       "Wall time of writing one batch of claude_code lines into "
                                       "the sessions table",
                                       buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 5))
    SESSION_INGEST_FAILURES = Counter("tares_session_ingest_failures_total",
                                      "Batches of claude_code lines the sessions table could not "
                                      "take (the lines are stored; the next start catches the "
                                      "table up)")

_storage_probe = None
_licence_probe = None


# ── hooks, called from the daemon; each a no-op without the library ────────────
def events_ingested(source: str, n: int) -> None:
    if AVAILABLE and n:
        EVENTS.labels(source=source).inc(n)


def poll(source: str, outcome: str) -> None:
    if AVAILABLE:
        POLLS.labels(source=source, outcome=outcome).inc()


def source_state(source: str, state: str) -> None:
    """One-hot: the state the source is in reads 1, the others 0."""
    if AVAILABLE:
        for s in ("ok", "error", "paused", "push"):
            SOURCE_STATE.labels(source=source, state=s).set(1 if s == state else 0)


def source_gone(source: str) -> None:
    if AVAILABLE:
        for s in ("ok", "error", "paused", "push"):
            try:
                SOURCE_STATE.remove(source, s)
            except KeyError:
                pass


def agent_run(agent: str, status: str, seconds: float | None = None) -> None:
    if AVAILABLE:
        AGENT_RUNS.labels(agent=agent, status=status).inc()
        if seconds is not None:
            AGENT_RUN_SECONDS.labels(agent=agent).observe(seconds)


def model_usage(provider: str | None, surface: str, calls: int, input_tokens: int,
                output_tokens: int, cost_usd: float | None) -> None:
    if not AVAILABLE:
        return
    p = provider or "anthropic"
    MODEL_CALLS.labels(provider=p, surface=surface).inc(calls)
    MODEL_TOKENS.labels(provider=p, surface=surface, direction="input").inc(input_tokens)
    MODEL_TOKENS.labels(provider=p, surface=surface, direction="output").inc(output_tokens)
    if cost_usd is None:
        MODEL_UNPRICED.labels(provider=p).inc(calls)
    elif cost_usd > 0:
        MODEL_SPEND.labels(provider=p, surface=surface).inc(cost_usd)


def trigger_evaluation(seconds: float, fired: list) -> None:
    if AVAILABLE:
        TRIGGER_EVALS.inc()
        TRIGGER_EVAL_SECONDS.observe(seconds)
        for name, _key in fired:
            TRIGGER_FIRINGS.labels(trigger=name).inc()


def session_ingest(seconds: float, ok: bool = True) -> None:
    """One batch written to the sessions table (TR-1471)."""
    if AVAILABLE:
        SESSION_INGEST_SECONDS.observe(seconds)
        if not ok:
            SESSION_INGEST_FAILURES.inc()


def observe_request(route: str, method: str, status: str, seconds: float) -> None:
    """One HTTP request (TR-652). `route` is a route template or `static`/`unmatched`, never a
    raw path; `status` is the class (`2xx` .. `5xx`)."""
    if AVAILABLE:
        HTTP_SECONDS.labels(route=route, method=method, status=status).observe(seconds)


# The routes that report their phases, and the phases each one has (TR-1476). Every label set is
# made at zero on import, so the family is on /metrics before the first call.
ROUTE_PHASES = {
    "brief": ("pr_refresh", "ticket_read", "recorded_commands", "challenger", "decisions",
              "recurring", "repo_map", "render"),
    "builder_brief": ("pr_refresh", "ticket_read", "recorded_commands", "challenger", "milestone",
                      "dependencies", "agents_docs", "repo_map", "desk", "protocol_messages",
                      "render"),
    "merge_ready": ("pr_read", "review", "ci", "pr_files", "guardrails"),
}
if AVAILABLE:
    for _route, _phases in ROUTE_PHASES.items():
        for _name in _phases:
            PHASE_SECONDS.labels(route=_route, phase=_name)

_phase_route: contextvars.ContextVar = contextvars.ContextVar("tares_phase_route", default=None)
_phase_open: contextvars.ContextVar = contextvars.ContextVar("tares_phase_open", default=None)


@contextlib.contextmanager
def bind_route(route: str):
    """Name the route being served, so `step(name)` calls deeper down (a helper that does not know
    which route called it) report under it. Outside this block `step` does nothing."""
    tok = _phase_route.set(route)
    try:
        yield
    finally:
        _phase_route.reset(tok)


@contextlib.contextmanager
def phase(route: str, name: str):
    """Time the block as phase `name` of `route` (TR-1476). A phase inside it is left out of this
    one's time, so a route's phases add up to its time and a rate of one is not counted twice.
    The time is reported when the block raises too. A no-op without the client library."""
    if not AVAILABLE:
        yield
        return
    parent = _phase_open.get()
    frame = [0.0]                       # what the phases inside this one took
    tok = _phase_open.set(frame)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _phase_open.reset(tok)
        took = time.perf_counter() - t0
        if parent is not None:
            parent[0] += took
        PHASE_SECONDS.labels(route=route, phase=name).observe(max(0.0, took - frame[0]))


def step(name: str):
    """`phase` for the route `bind_route` named; a block that runs on no bound route is not timed."""
    route = _phase_route.get()
    return phase(route, name) if route else contextlib.nullcontext()


# Path prefixes the API answers under; a request outside them that matched no route is the console
# (its files and client-side routes), labelled `static` rather than one series per path.
_CONSOLE_CATCH_ALL = "/{path:path}"   # the daemon's last route, serving the console's index
_API_PREFIXES = ("/api", "/ingest", "/v1", "/mcp", "/health", "/read", "/subscribe", "/unsubscribe")


class RequestTiming:
    """Pure ASGI middleware timing every HTTP request into tares_http_request_duration_seconds
    (TR-652). Not Starlette's BaseHTTPMiddleware, which buffers streaming responses. Added last so
    it is outermost and also times requests the auth guard turns away."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") == "/metrics":
            await self.app(scope, receive, send)
            return
        status = 500   # the app raised before it started a response
        t0 = time.perf_counter()

        async def send_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_status)
        finally:
            route = getattr(scope.get("route"), "path", None)
            if route is None or route == _CONSOLE_CATCH_ALL:
                path = scope.get("path") or ""
                route = "unmatched" if path.startswith(_API_PREFIXES) else "static"
            observe_request(route, scope.get("method", ""), f"{status // 100}xx",
                            time.perf_counter() - t0)


RUN_STATUSES = ("ok", "empty", "exhausted", "capped", "failed")


def prime(sources=(), triggers=(), agents=(), providers=()) -> None:
    """Create every counter's label set at zero before its first event. A counter that only
    appears when something happens makes `increase()` miss that first event (Prometheus sees a
    series that starts at 1); with the zero sample present, rates and increases are right from
    the first run, the first firing, the first poll. Called on start and on every catalog reload."""
    if not AVAILABLE:
        return
    for s in sources:
        EVENTS.labels(source=s)
        for o in ("ok", "error", "paused"):
            POLLS.labels(source=s, outcome=o)
    for t in triggers:
        TRIGGER_FIRINGS.labels(trigger=t)
    for a in agents:
        for st in RUN_STATUSES:
            AGENT_RUNS.labels(agent=a, status=st)
    for p in providers:
        MODEL_UNPRICED.labels(provider=p)
        for surface in ("agent", "ask"):
            MODEL_CALLS.labels(provider=p, surface=surface)
            MODEL_SPEND.labels(provider=p, surface=surface)
            for d in ("input", "output"):
                MODEL_TOKENS.labels(provider=p, surface=surface, direction=d)


def set_storage_probe(fn) -> None:
    """`fn()` returns {db_bytes, max_bytes, pct_used, paused, events}; read on every scrape, so the
    gauges cost nothing between scrapes."""
    global _storage_probe
    _storage_probe = fn


def set_licence_probe(fn) -> None:
    """`fn()` returns the licence state (tares.licence.peek: the remembered answer, no database read); read on every scrape."""
    global _licence_probe
    _licence_probe = fn


def set_info(version: str | None) -> None:
    if AVAILABLE:
        INFO.labels(version=version or "unknown").set(1)


async def watch_event_loop(stop: asyncio.Event | None = None, interval: float = 1.0) -> None:
    """Measure how late a one-second timer runs: anything blocking the loop (a long DuckDB
    scan, a synchronous connector) shows up as lag. A cheap, always-on stall detector."""
    while not (stop and stop.is_set()):
        t0 = time.monotonic()
        await asyncio.sleep(interval)
        lag = max(0.0, time.monotonic() - t0 - interval)
        if AVAILABLE:
            LOOP_LAG.set(lag)
            if lag > 0.5:
                LOOP_STALLS.inc()


def render() -> tuple[bytes, str]:
    """The scrape body and its content type. Refreshes the storage gauges first."""
    if not AVAILABLE:
        raise RuntimeError("prometheus_client is not installed")
    if _storage_probe is not None:
        try:
            st = _storage_probe() or {}
            DB_BYTES.set(float(st.get("db_bytes") or 0))
            DB_MAX_BYTES.set(float(st.get("max_bytes") or 0))
            STORAGE_PCT.set(float(st.get("pct_used") or 0))
            INGEST_PAUSED.set(1 if st.get("paused") else 0)
            EVENTS_STORED.set(float(st.get("events") or 0))
        except Exception:   # a scrape must never fail over a gauge
            pass
    if _licence_probe is not None:
        try:
            lic = _licence_probe() or {}
            valid = bool(lic.get("valid"))
            LICENCE_VALID.set(1 if valid else 0)
            LICENCE_SEATS.set(float(lic.get("seats") or 0) if valid else 0)
            LICENCE_USED.set(float(lic.get("used") or 0))
            LICENCE_DAYS_LEFT.set(float(lic.get("days_left") or 0) if valid else 0)
        except Exception:   # a scrape must never fail over a gauge
            pass
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
