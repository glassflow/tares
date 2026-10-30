"""The one AI SRE cell the tests and the console checks share (TR-742).

build(store, now, ...) writes a cell with invented names only:
  prod-otlp  (otlp, primary label service)   logs for edge-proxy and service-a, spans and metrics
             for service-a, and two old lines (2 days back) for service-a-receiver
  docs-site  (vercel, primary label project) logs for docs-site
  github     (github_app, label repo)         commits on example-org/service-a, which
             arrive by webhook, so the source never polls and the tests need no network
The counts (see the returned dict) are fixed, so a test can name them. `events=N` adds N more
edge-proxy and service-a log lines spread over the hours between 1 and 24 back: they go in through
the store's own append in batches of 5,000, the same code path as real ingest, so a 200,000-line
cell takes about 40 seconds to build and no test needs a second way in.

seed_cell(data_dir, edition="sre", **kw) is what ui/tests/seed-sre-*.py call.
"""
import os
from datetime import timedelta

from tares import edition as edition_mod
from tares.connectors import source_type_for
from tares.envelope import Envelope
from tares.store import Store

PROJECT = "project-x-watch"
BATCH = 5000


def _otlp(now, minutes_ago, service, event_type, text, labels=None, payload=None):
    t = now - timedelta(minutes=minutes_ago)
    return Envelope(source="prod-otlp", source_type=source_type_for("otlp"), key_value=service,
                    event_type=event_type, text=text, event_time=t, payload=payload or {},
                    labels={"service": service, **(labels or {})}, ingest_time=t)


def _edge(now, minutes_ago, status="200"):
    return _otlp(now, minutes_ago, "edge-proxy", "INFO" if status < "500" else "ERROR",
                 f"GET /ui {status}",
                 {"proxy_upstream_name": "default-service-a-ui-80", "namespace": "default",
                  "nginx_status_code": status})


def build(store: Store, now, *, watcher: bool = False, investigator: bool = False,
          quiet_hours: int = 0, events: int = 0) -> dict:
    """Write the cell into `store` as of `now`. Returns the names it made."""
    store.upsert_catalog_source("prod-otlp", source_type_for("otlp"), "otlp", "5s", {
        "labels": [{"name": "service", "field": "service.name", "primary": True},
                   {"name": "namespace", "field": "namespace"}]})
    store.upsert_catalog_source("docs-site", source_type_for("vercel"), "vercel", "1m", {
        "labels": [{"name": "project", "field": "project", "primary": True}]})
    store.upsert_catalog_source("github", source_type_for("github_app"), "github_app", "1h", {
        "labels": [{"name": "repo", "field": "repo", "primary": True}]})

    env = [
        # edge-proxy: 3 lines in the last 10 minutes, 2 more earlier today
        _edge(now, 2), _edge(now, 5, "502"), _edge(now, 8), _edge(now, 180), _edge(now, 240),
        # service-a: a log, a span and a metric point in the last 10 minutes, the same earlier
        _otlp(now, 1, "service-a", "INFO", "started job 41"),
        _otlp(now, 4, "service-a", "span", "POST /jobs", payload={"traceId": "t1"}),
        _otlp(now, 6, "service-a", "jobs_in_flight", "jobs_in_flight 3",
              payload={"metricType": "gauge", "value": 3}),
        _otlp(now, 240, "service-a", "ERROR", "job 40 failed"),
        _otlp(now, 300, "service-a", "span", "POST /jobs", payload={"traceId": "t0"}),
        _otlp(now, 120, "service-a", "jobs_in_flight", "jobs_in_flight 1",
              payload={"metricType": "gauge", "value": 1}),
        # service-a-receiver: old, and nothing since
        _otlp(now, 2 * 24 * 60, "service-a-receiver", "INFO", "received batch 7"),
        _otlp(now, 2 * 24 * 60 + 5, "service-a-receiver", "INFO", "received batch 6"),
    ]
    for minutes_ago, text in ((20, "deployed docs-site"), (360, "built docs-site")):
        t = now - timedelta(minutes=minutes_ago)
        env.append(Envelope(source="docs-site", source_type=source_type_for("vercel"),
                            key_value="docs-site", event_type="deployment", text=text,
                            event_time=t, payload={}, labels={"project": "docs-site"},
                            ingest_time=t))
    for minutes_ago, text in ((90, "fix retry in job runner"), (600, "add job metrics")):
        t = now - timedelta(minutes=minutes_ago)
        env.append(Envelope(source="github", source_type=source_type_for("github_app"),
                            key_value="example-org/service-a", event_type="commit", text=text,
                            event_time=t, payload={}, labels={"repo": "example-org/service-a"},
                            ingest_time=t))
    store.append(env)

    # `events` more lines between 1 and 24 hours back, so the 10-minute counts above stay fixed
    for start in range(0, events, BATCH):
        batch = []
        for i in range(start, min(start + BATCH, events)):
            minutes_ago = 60 + (i * (23 * 60 - 10)) // max(events, 1)
            batch.append(_edge(now, minutes_ago) if i % 2 == 0 else
                         _otlp(now, minutes_ago, "service-a", "INFO", f"handled request {i}"))
        store.append(batch)

    made = {"sources": ["prod-otlp", "docs-site", "github"],
            "services": ["edge-proxy", "service-a", "docs-site", "service-a-receiver"],
            "events": events}
    if watcher or investigator:
        made.update(_agents(store, now, investigator, quiet_hours))
    return made


def _agents(store: Store, now, investigator: bool, quiet_hours: int) -> dict:
    """A watcher on a 10-minute schedule reading prod-otlp, in a project of its own."""
    uid = "prj-" + PROJECT
    store.create_project(uid, "custom", PROJECT, {}, goal="Keep service-a healthy")
    store.upsert_catalog_trigger("watch-prod", ["prod-otlp"], {"every": "10m"}, {}, "10m")
    handoffs = [{"verdict": "investigate", "agent": "investigator", "cooldown": "1h"}] \
        if investigator else None
    store.upsert_catalog_agent(
        "watcher", "watch-prod", "Look at the last 10 minutes of prod-otlp and say what changed.",
        budget_usd=25.0, budget_period="month", concludes=True, handoffs=handoffs)
    names = {"agents": ["watcher"], "triggers": ["watch-prod"], "project": uid}
    if investigator:
        store.upsert_catalog_agent(
            "investigator", "watch-prod", "Find the cause of what the watcher flagged.",
            budget_usd=25.0, concludes=True)
        names["agents"].append("investigator")
    store.put_in_project("source", "prod-otlp", uid)
    store.put_in_project("trigger", "watch-prod", uid)
    for a in names["agents"]:
        store.put_in_project("agent", a, uid)
    store.set_wake(uid, "watch-prod", "watcher", enabled=True)
    if investigator:
        store.set_handoffs(uid, "watcher", handoffs)
    store.set_fired("watch-prod", "watch-prod", now - timedelta(hours=quiet_hours, minutes=3))
    return names


def seed_cell(data_dir: str, edition: str = "sre", **kw) -> dict:
    """What ui/tests/seed-sre-*.py call: a cell on disk the daemon can start on."""
    from tares.envelope import now_utc
    os.makedirs(data_dir, exist_ok=True)
    store = Store(os.path.join(data_dir, "tares.duckdb"))
    try:
        store.set_setting(edition_mod.EDITION_SETTING, edition)
        return build(store, kw.pop("now", None) or now_utc(), **kw)
    finally:
        store.con.close()
