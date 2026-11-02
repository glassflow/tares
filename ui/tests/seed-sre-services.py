"""Seed a scratch AI SRE cell for sre-services.e2e.cjs (TR-750): the Services list and a page per service.
The shared fixture (tests/fixtures/sre_cell.py) writes the cell: edge-proxy, service-a, service-a-receiver
(silent), service-a-ui (seen only through edge-proxy), docs-site, service-b and 22 more services, a
watcher on a schedule, and the repo example-org/service-a with commits, pushes and a merge. Then edge-proxy
is linked to that repo, so its chart has deploys. The watcher has checked a few minutes ago (so the
services are watched), has raised something about edge-proxy, and service-a has an open incident. Invented
names only.
usage: python seed-sre-services.py <data-dir>   (run from the repo root; prints the made names as JSON)"""
import json, os, sys, uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "tests"))

from fixtures.sre_cell import seed_cell  # noqa: E402
from tares.store import Store  # noqa: E402

home = sys.argv[1]
made = seed_cell(home, "sre", blind=True, watcher=True, services=22)
store = Store(os.path.join(home, "tares.duckdb"))
try:
    store.set_service("edge-proxy", "seed", repos=["example-org/service-a"])
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    uid = made["project"]

    def finished(rid, agent, service, minutes_ago, **kw):
        store.start_agent_run(rid, agent, "watch-prod", "", service, "h", project=uid,
                              **({"woken_by": "handoff"} if agent == "investigator" else {}))
        store.finish_agent_run(rid, "ok", **kw)
        with store._swrite:
            store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?",
                             [now - timedelta(minutes=minutes_ago), rid])

    finished("seed-check", "watcher", "", 3)
    finished("seed-raise", "watcher", "edge-proxy", 20, finding="edge-proxy returns errors.",
             outcome="finding", verdict="investigate")
    inc = store.create_incident("service-a", uid, now - timedelta(minutes=40))
    rid = "seed-" + uuid.uuid4().hex[:8]
    finished(rid, "watcher", "service-a", 40, finding="service-a returns errors.",
             outcome="finding", verdict="rca")
    store.add_incident_note(inc["id"], rid, now - timedelta(minutes=40))
finally:
    store.con.close()
print(json.dumps(made))
