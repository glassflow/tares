"""Seed a scratch AI SRE cell for sre-incidents.e2e.cjs (TR-751): the Incidents list and the Incident page
on a cell with history. Invented names only. Writes straight into the database (no daemon needed), the
way tests/test_sre_incidents.py does, then backdates the times so the rows read as a cell with history.
  open-a   edge-proxy, open 38 minutes, two notes (the second joined), raised by the watcher, a Slack message
  open-g   service-a, open 5 minutes, a note nobody raised (cannot be answered), no Slack message
  open-l   docs-site, open 30 minutes, a long note (headings, over 1200 characters)
  onit-b   docs-site, Alex took it 20 minutes ago
  res-c    service-a-receiver, resolved by Alex two days ago
  res-d    edge-proxy, resolved itself after 12 minutes
  res-e    edge-proxy, from before incidents were tracked
  22 more resolved ones (25 in all, the page holds 20), and a finding that did not become an incident
Prints a JSON map of names to ids on the last line.
usage: python seed-sre-incidents.py <data-dir> [port mcp-port ui-dist tares-bin, unused]"""
import json, os, sys
from datetime import timedelta

sys.path.insert(0, os.getcwd())
from tests.fixtures.sre_cell import seed_cell                      # noqa: E402
from tares import incidents                                         # noqa: E402
from tares.envelope import now_utc                                  # noqa: E402
from tares.store import Store                                       # noqa: E402

home = sys.argv[1]
base = now_utc().replace(microsecond=0)
made = seed_cell(home, "sre", now=base, watcher=True, investigator=True)
store = Store(os.path.join(home, "tares.duckdb"))
uid = made["project"]
ids = {}


def run(rid, agent, key, finding, at, parent=None, verdict="rca", status="ok", outcome="finding", **kw):
    store.start_agent_run(rid, agent, "watch-prod", "", key, "h", woken_by="handoff" if parent else None,
                          parent_run_id=parent, project=uid)
    store.finish_agent_run(rid, status, finding=finding, outcome=outcome, verdict=verdict, **kw)
    with store._swrite:
        store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, rid])


def backdate(iid, **cols):
    with store._swrite:
        for col, at in cols.items():
            store.sc.execute(f"UPDATE incidents SET {col} = ? WHERE id = ?", [at, iid])


def incident(service, opened, runs, **kw):
    inc = store.create_incident(service, uid, opened, **kw)
    for rid, at in runs:
        store.add_incident_note(inc["id"], rid, at)
    return inc["id"]


try:
    t = base - timedelta(minutes=38)
    run("w-a", "watcher", "edge-proxy", "502s on edge-proxy rose to 12x.", t, verdict="investigate")
    run("i-a1", "investigator", "edge-proxy",
        "Let me look at the logs. The deploy of service-a left the proxy on old pods.\n\n"
        "Cause: the rollout never finished.", t + timedelta(minutes=1), parent="w-a",
        failing="edge-proxy returns 502 on /ui", likely_cause="The rollout never finished",
        next_step="Roll the deploy back")
    run("i-a2", "investigator", "edge-proxy", "The proxy still points at removed pods.",
        base - timedelta(minutes=20), parent="w-a")
    ids["open-a"] = incident("edge-proxy", t, [("i-a1", t), ("i-a2", base - timedelta(minutes=20))])
    store.set_note_slack("i-a1", "C1", "1790000000.000001")
    store.set_incident_slack(ids["open-a"], "C1", "1790000000.000001")

    t = base - timedelta(minutes=5)
    run("i-g", "investigator", "service-a", "Jobs fail since the last deploy.", t, verdict="rca",
        outcome="no_op")
    ids["open-g"] = incident("service-a", t, [("i-g", t)])

    t = base - timedelta(minutes=30)
    long_note = "# What happened\n\n" + " ".join(
        f"Build {n} of docs-site stopped at the image step, and the logs show the registry refusing the push."
        for n in range(1, 9)) + "\n\n# What I checked\n\n" + " ".join(
        f"Check {n}: the build host had room, the token was valid, and the registry answered other pushes."
        for n in range(1, 9)) + "\n\n# What to do\n\nAsk the registry owner to raise the push limit, then build again."
    assert len(long_note) > 1200
    run("i-l", "investigator", "docs-site", long_note, t)
    ids["open-l"] = incident("docs-site", t, [("i-l", t)])

    t = base - timedelta(minutes=60)
    run("i-b", "investigator", "docs-site", "Builds of docs-site fail.", t)
    ids["onit-b"] = incident("docs-site", t, [("i-b", t)])
    incidents.take(store, ids["onit-b"], "alex", "Alex", "console")
    backdate(ids["onit-b"], owner_at=base - timedelta(minutes=20))

    t = base - timedelta(days=2, hours=1)
    run("i-c", "investigator", "service-a-receiver", "The receiver stopped.", t)
    ids["res-c"] = incident("service-a-receiver", t, [("i-c", t)])
    incidents.resolve(store, ids["res-c"], "alex", "Alex", "console")
    backdate(ids["res-c"], resolved_at=base - timedelta(days=2))

    t = base - timedelta(hours=5)
    ids["res-d"] = incident("edge-proxy", t, [], state="resolved", resolved_at=t + timedelta(minutes=12),
                            resolution="itself")
    t = base - timedelta(days=9)
    ids["res-e"] = incident("edge-proxy", t, [], state="resolved", resolved_at=t + timedelta(hours=1),
                            resolution="before_tracking")
    for i in range(22):
        t = base - timedelta(days=3, minutes=i * 7)
        incident("service-a", t, [], state="resolved", resolved_at=t + timedelta(minutes=3),
                 resolution="itself")

    run("w-r", "watcher", "docs-site", "docs-site builds are slow.", base - timedelta(minutes=3),
        verdict="investigate")
finally:
    store.con.close()
print(json.dumps(ids))
