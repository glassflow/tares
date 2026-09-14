"""The daemon's own counters, served at `GET /metrics` in the Prometheus text format (TR-308).

What a cell is doing, for a fleet dashboard and alerts: events in, poll errors, source health,
agent runs and their outcomes, model calls, tokens and spend by provider, database size against
its limit, trigger evaluations and how long they take, and the event loop's lag. Labels name the
source, agent, provider or status; never an entity key or anything from a payload.

Every hook here is a no-op when the client library is missing, so nothing else in the daemon
guards its calls. The endpoint answers 404 in that case.
"""
from __future__ import annotations

import asyncio
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
    INFO = Gauge("tares_info", "Build information", ["version"])

_storage_probe = None


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
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
