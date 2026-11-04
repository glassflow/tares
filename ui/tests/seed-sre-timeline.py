"""Seed a scratch AI SRE cell for sre-timeline.e2e.cjs (TR-752): the Timeline page as a cell with a
bad half hour, a deploy, alerts, checks and an incident shows it. Invented names only. Every time is
relative to the moment of seeding, on 10-minute edges (B is now rounded down to ten minutes), so the
windows are the ones the cell draws:
  the base cell (tests/fixtures/sre_cell.py): edge-proxy, service-a, docs-site, service-a-receiver
      (silent for two days), a github source and a watcher on a 10-minute schedule;
  edge-proxy: 10 lines a window from B-120 to B-40, a warn window [B-40, B-30) of 61 lines and 4
      errors, a bad window [B-30, B-20) of about 260 lines and 41 errors (one line of 500 characters among
      the newest), 30 lines and 3 errors after it;
  service-a: 10 lines a window for two hours (one folded "normal" item with lines behind it);
  a push to the repo linked to edge-proxy at B-37, a push to a repo linked to service-a at B-95;
  an alertmanager source: a page alert on edge-proxy firing at B-29, a warn alert on service-a that
      fired at B-95 and resolved at B-85, a warn alert on docs-site that fired 20 hours ago and
      resolved 19 hours ago, and an old one 40 hours ago;
  three quiet checks on service-a, a check that found errors on edge-proxy, an incident on
      edge-proxy opened at B-22 that Kim took at B-18;
  burst-01 to burst-03: from 7 to 24 hours back each 10-minute window alternates between 12 and
      40 lines, so a 24-hour view has more than 200 items and a second page.
usage: python seed-sre-timeline.py <data-dir>
Prints a JSON map of what it made on the last line. Nothing here starts a daemon."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.getcwd()
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from fixtures.sre_cell import _otlp, seed_cell  # noqa: E402
from tares.connectors import source_type_for  # noqa: E402
from tares.envelope import Envelope  # noqa: E402
from tares.store import Store  # noqa: E402

home = sys.argv[1]
now = datetime.now(timezone.utc)
B = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
made = seed_cell(home, edition="sre", now=now, watcher=True)
uid = made["project"]


def ago(minutes):
    return B - timedelta(minutes=minutes)


def lines(service, k, n, errors=0, text="handled request"):
    """`n` lines of `service` inside the window k (0 is B-10 to B, 1 is B-20 to B-10, ...)."""
    return [_otlp(B, 10 * k + 0.2 + (i * 9.5) / n, service, "ERROR" if i < errors else "INFO",
                  f"{text} {i}") for i in range(n)]


def other(source, connector, key, kind, text, when, labels):
    return Envelope(source=source, source_type=source_type_for(connector), key_value=key,
                    event_type=kind, text=text, event_time=when, payload={}, labels=labels,
                    ingest_time=when)


store = Store(os.path.join(home, "tares.duckdb"))
try:
    store.upsert_catalog_source("alerts-prod", source_type_for("alertmanager"), "alertmanager", "5s",
                                {"labels": [{"name": "service", "field": "service", "primary": True}]})
    store.set_service("edge-proxy", "tester", repos=["example-org/edge-proxy"])
    store.set_service("service-a", "tester", repos=["example-org/service-a"])

    env = []
    for k in range(4, 12):                    # B-120 to B-40: steady
        env += lines("edge-proxy", k, 10)
    env += lines("edge-proxy", 3, 61, 4)      # [B-40, B-30): up, with the first errors
    bad = lines("edge-proxy", 2, 258, 41)     # [B-30, B-20): the bad window (the alert is the 260th event)
    bad += [_otlp(B, 20.05, "edge-proxy", "ERROR", "GET /ui 502"),
            _otlp(B, 20.1, "edge-proxy", "ERROR", "GET /ui 502"),
            _otlp(B, 20.15, "edge-proxy", "ERROR",
                  "upstream connect error or disconnect/reset before headers " + "x" * 440 + " end")]
    env += bad
    env += lines("edge-proxy", 1, 30, 3)      # [B-20, B-10): falling back
    for k in range(0, 12):
        env += lines("service-a", k, 10)
    for name in ("burst-01", "burst-02", "burst-03"):
        for k in range(43, 143):
            n = 12 if k % 2 else 40
            env += lines(name, k, n, 1 if k % 7 == 0 else 0)
    env += [
        other("github", "github_app", "example-org/edge-proxy", "push", "push to main", ago(37),
              {"repo": "example-org/edge-proxy", "branch": "main", "author": "Kim", "commits": "3"}),
        other("github", "github_app", "example-org/service-a", "push", "push to main", ago(95),
              {"repo": "example-org/service-a", "branch": "main", "author": "Alex", "commits": "1"}),
        other("alerts-prod", "alertmanager", "edge-proxy", "HighErrorRate", "error rate high", ago(29),
              {"service": "edge-proxy", "severity": "page", "status": "firing"}),
        other("alerts-prod", "alertmanager", "service-a", "SlowJobs", "jobs are slow", ago(95),
              {"service": "service-a", "severity": "warning", "status": "firing"}),
        other("alerts-prod", "alertmanager", "service-a", "SlowJobs", "jobs are fine", ago(85),
              {"service": "service-a", "severity": "warning", "status": "resolved"}),
        other("alerts-prod", "alertmanager", "docs-site", "BuildFailing", "the build failed", ago(20 * 60),
              {"service": "docs-site", "severity": "warning", "status": "firing"}),
        other("alerts-prod", "alertmanager", "docs-site", "BuildFailing", "the build passes", ago(19 * 60),
              {"service": "docs-site", "severity": "warning", "status": "resolved"}),
        other("alerts-prod", "alertmanager", "docs-site", "BuildFailing", "the build failed", ago(40 * 60),
              {"service": "docs-site", "severity": "warning", "status": "firing"}),
        other("alerts-prod", "alertmanager", "docs-site", "BuildFailing", "the build passes", ago(39 * 60),
              {"service": "docs-site", "severity": "warning", "status": "resolved"}),
    ]
    store.append(env)

    def run(rid, key, minutes_ago, finding, outcome, verdict=None):
        store.start_agent_run(rid, "watcher", "watch-prod", "", key, "h", project=uid)
        store.finish_agent_run(rid, "ok", finding=finding, outcome=outcome, verdict=verdict)
        with store._swrite:
            store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [ago(minutes_ago), rid])

    run("r1", "service-a", 70, "Nothing unusual on service-a.", "no_op")
    run("r2", "service-a", 60, "Nothing unusual on service-a.", "no_op")
    run("r3", "service-a", 50, "Nothing unusual on service-a.", "no_op")
    run("r4", "edge-proxy", 27, "Errors on edge-proxy rose from none to 12.", "finding", "investigate")
    inc = store.create_incident("edge-proxy", uid, ago(22))
    store.add_incident_note(inc["id"], "r4", ago(22))
    store.log_incident(inc["id"], "opened", None, "watcher", "runner", ago(22))
    store.set_incident_state(inc["id"], "on_it", "u1", "Kim", "ui", at=ago(18))
finally:
    store.con.close()

print(json.dumps({"base": B.isoformat(), "project": uid, "incident": inc["id"],
                  "services": made["services"] + ["burst-01", "burst-02", "burst-03"]}))
