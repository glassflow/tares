"""Seed a scratch AI SRE cell for sre-home.e2e.cjs (TR-749): the AI SRE home page, one state at a time.
The shared fixture (tests/fixtures/sre_cell.py) writes the cell: edge-proxy, service-a, service-a-receiver
(silent), docs-site and a watcher on a schedule, with the repo example-org/service-a. This script then
writes the runs, findings and incidents of one state:
  watcher-off     the watcher used its $25 budget this month; its last check was a while ago
  all-quiet       the watcher checked a few minutes ago and found nothing; one old thing was raised
  one-incident    all-quiet, and service-a has an open incident with a note and a post in Slack
  many-incidents  five open incidents (the page shows three and says "2 more")
  new-cell        nothing connected at all
  silent-service  all-quiet on a cell that also has the blind-spot data (service-a-receiver is silent)
Invented names only.
usage: python seed-sre-home.py <data-dir> <state>   (run from the repo root; prints the made names as JSON)"""
import json, os, sys, uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))

from fixtures.sre_cell import seed_cell  # noqa: E402
from tares import edition as edition_mod  # noqa: E402
from tares.store import Store  # noqa: E402

STATES = ("watcher-off", "all-quiet", "one-incident", "many-incidents", "new-cell", "silent-service")
home, state = sys.argv[1], sys.argv[2]
if state not in STATES:
    raise SystemExit(f"state must be one of {', '.join(STATES)}")

if state == "new-cell":
    os.makedirs(home, exist_ok=True)
    store = Store(os.path.join(home, "tares.duckdb"))
    try:
        store.set_setting(edition_mod.EDITION_SETTING, "sre")
    finally:
        store.con.close()
    print(json.dumps({"services": []}))
    raise SystemExit(0)

made = seed_cell(home, "sre", blind=state != "all-quiet", watcher=True, investigator=True)
store = Store(os.path.join(home, "tares.duckdb"))
try:
    store.set_service("edge-proxy", "seed", repos=["example-org/service-a"])
    store.set_service("service-a", "seed", repos=["example-org/service-a"])   # so its incident chart has deploys
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    uid = made["project"]

    def at(minutes_ago):
        """A time `minutes_ago` back, but never before the start of this month."""
        return max(now - timedelta(minutes=minutes_ago), month + timedelta(seconds=1))

    def finished(rid, agent, service, minutes_ago, cost=0.0, **kw):
        store.start_agent_run(rid, agent, "watch-prod", "", service, "h", project=uid,
                              **({"woken_by": "handoff"} if agent == "investigator" else {}))
        store.finish_agent_run(rid, "ok", **kw)
        with store._swrite:
            store.sc.execute("UPDATE agent_runs SET started_at = ?, cost_usd = ? WHERE id = ?",
                             [at(minutes_ago), cost, rid])

    def raised(rid, service, minutes_ago):
        finished(rid, "watcher", service, minutes_ago, finding=f"{service} returns errors.",
                 outcome="finding", verdict="investigate")

    def open_incident(service, minutes_ago, slack=True):
        opened = at(minutes_ago)
        inc = store.create_incident(service, uid, opened)
        rid = "seed-" + uuid.uuid4().hex[:8]
        finished(rid, "investigator", service, minutes_ago, finding=f"{service} returns errors.",
                 outcome="finding", verdict="rca", failing=f"1 in 12 requests to {service} returns 502.",
                 likely_cause=f"{service} restarted after a push to main.",
                 next_step=f"Roll back the last deploy of {service}.")
        store.add_incident_note(inc["id"], rid, opened)
        if slack:
            store.set_incident_slack(inc["id"], "ops-alerts", f"{int(opened.timestamp()) + 60}.000100")
        return inc

    if state == "watcher-off":
        # the watcher's budget is $25 a month, and one check cost all of it
        finished("seed-last", "watcher", "", 360, cost=25.0, finding="Traffic steady, status codes normal. Nothing to raise.",
                 outcome="no_op")
        raised("seed-old-1", "edge-proxy", 3 * 24 * 60)
    else:
        finished("seed-check", "watcher", "", 3, cost=0.005, finding="Traffic steady, status codes normal. Nothing to raise.",
                 outcome="no_op")
        raised("seed-old-1", "edge-proxy", 3 * 24 * 60)
        raised("seed-old-2", "docs-site", 4 * 24 * 60)
    if state == "one-incident":
        open_incident("service-a", 40)
    if state == "many-incidents":
        for n, svc in enumerate(["service-a", "edge-proxy", "docs-site", "service-b", "service-a-receiver"]):
            open_incident(svc, 40 + 15 * n, slack=n % 2 == 0)
finally:
    store.con.close()
print(json.dumps(made))
