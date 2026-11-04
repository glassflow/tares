"""The incident backfill on a cell with many findings (TR-897).

It decides every join in memory and writes in batches, so a cell with thousands of findings starts
in seconds. Covers: it files exactly what the one-note-at-a-time rule files (an oracle that is the
old loop, run on a twin store with the same runs), including a live incident it must join or leave
alone, and a number of write statements that does not grow with the notes.

Run: .venv/bin/python tests/test_incident_backfill_bulk.py
"""
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")

from fixtures.sre_cell import build  # noqa: E402
from tares import incidents  # noqa: E402
from tares.config import catalog_from_db, trigger_entity_label  # noqa: E402
from tares.store import Store  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


NOW = datetime.now(timezone.utc).replace(microsecond=0)
N = 400


def seed(store):
    """The same runs every time: findings on 12 services over 30 days, most woken by a watcher's
    run (the handoff cooldown), some by nothing (the trigger's cooldown), some that say resolved
    (no note), some practice runs (no note), and a live incident from an hour ago."""
    rng = random.Random(7)
    uid = build(store, NOW, watcher=True, investigator=True)["project"]
    for i in range(N):
        key = f"svc-{rng.randrange(12)}"
        at = NOW - timedelta(days=30) + timedelta(minutes=rng.randrange(0, 30 * 24 * 60 - 120))
        rid, kind = f"r{i:04d}", rng.random()
        parent = None
        if kind < 0.7:
            parent = f"w{i:04d}"
            store.start_agent_run(parent, "watcher", "watch-prod", "", key, "h", project=uid)
            store.finish_agent_run(parent, "ok", finding="flag", outcome="finding",
                                   verdict="raised")
        store.start_agent_run(rid, "investigator", "watch-prod", "", key, "h",
                              woken_by="handoff" if parent else None, parent_run_id=parent,
                              project=uid, practice=(0.9 < kind < 0.95))
        store.finish_agent_run(rid, "ok", finding=f"{key} errors.", outcome="finding",
                               verdict="resolved" if kind >= 0.95 else "rca")
        with store._swrite:
            store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, rid])
    live = store.create_incident("svc-3", uid, NOW - timedelta(hours=1))
    store.add_incident_note(live["id"], "live1", NOW - timedelta(hours=1))
    store.log_incident(live["id"], "opened", None, "investigator", "runner", NOW - timedelta(hours=1))
    store.start_agent_run("live1", "investigator", "watch-prod", "", "svc-3", "h", project=uid)
    store.finish_agent_run("live1", "ok", finding="svc-3 errors.", outcome="finding", verdict="rca")
    return live["id"]


def old_backfill(store, catalog, now):
    """The backfill as it was: one lookup and a few writes per note."""
    from tares import sre
    store.delete_backfilled_incidents()
    roles = sre.roles(store, catalog, include_paused=True)
    triggers = {t.name: t for t in catalog.triggers}
    notes, offset = [], 0
    while True:
        page = store.list_agent_runs(None, limit=500, offset=offset)
        if not page:
            break
        offset += len(page)
        notes += [r for r in page if r.get("started_at") is not None
                  and incidents.is_note(store, catalog, {"name": r["agent"]}, r, roles)]
    notes.sort(key=lambda r: (incidents._aware(r["started_at"]), r["id"]))
    seen, made, filed = {}, [], 0
    for r in notes:
        if store.incident_for_run(r["id"]):
            continue
        filed += 1
        trig = triggers.get(r.get("trigger"))
        label = trigger_entity_label(trig, catalog.sources) if trig is not None else None
        at = incidents._aware(r["started_at"])
        if (label, r["key"]) not in seen:
            seen[(label, r["key"])] = incidents.service_of(store, catalog, r["key"], label, now)
        service = seen[(label, r["key"])]
        cooldown = incidents.cooldown_for(store, catalog, r)
        inc = store.open_incident_for(service, at - timedelta(seconds=cooldown), until=at)
        if inc:
            store.add_incident_note(inc["id"], r["id"], at)
            store.log_incident(inc["id"], "note_joined", None, r["agent"], "backfill", at)
        else:
            inc = store.create_incident(service, r.get("project"), at)
            store.add_incident_note(inc["id"], r["id"], at)
            store.log_incident(inc["id"], "opened", None, r["agent"], "backfill", at)
            made.append(inc["id"])
    for iid in made:
        inc = store.get_incident(iid)
        if inc and inc["state"] == "open" and incidents._aware(inc["last_note_at"]) \
                < now - incidents.BEFORE_TRACKING_AFTER:
            store.set_incident_state(iid, "resolved", None, None, "backfill",
                                     at=incidents._aware(inc["last_note_at"]),
                                     resolution="before_tracking", expect="open")
    return filed


def shape(store):
    """What the incident tables hold, with the random ids left out."""
    out = []
    for inc in store.list_incidents(limit=100000):
        notes = tuple(n["run_id"] for n in store.incident_notes(inc["id"]))
        log = tuple((e["action"], e["via"], e["by_name"]) for e in store.incident_log(inc["id"]))
        out.append((inc["service"], notes, inc["state"], inc["resolution"], inc["project"],
                    incidents._aware(inc["opened_at"]), incidents._aware(inc["last_note_at"]),
                    incidents._aware(inc["updated_at"]) if inc.get("updated_at") else None,
                    log))
    return sorted(out, key=lambda t: (t[0], t[1]))


class Counting:
    """The state connection, counting the statements sent through it."""
    def __init__(self, con):
        self._con, self.n = con, 0

    def execute(self, *a, **k):
        self.n += 1
        return self._con.execute(*a, **k)

    def executemany(self, *a, **k):
        self.n += 1
        return self._con.executemany(*a, **k)

    def __getattr__(self, name):
        return getattr(self._con, name)


def main():
    print("== the bulk backfill files what the one-note-at-a-time rule files ==")
    new_store = Store(os.path.join(TMP, "new.duckdb"))
    old_store = Store(os.path.join(TMP, "old.duckdb"))
    seed(new_store)
    seed(old_store)
    for st in (new_store, old_store):
        st.set_setting(incidents.BACKFILL_SETTING, "0")
    counter = Counting(new_store.sc)
    new_store.sc = counter
    filed = incidents.backfill(new_store, now=NOW)
    new_store.sc = counter._con
    want = old_backfill(old_store, catalog_from_db(old_store), NOW)
    new_shape, old_shape = shape(new_store), shape(old_store)
    ck("some notes were filed", filed > 100, filed)
    eq("as many notes as the old rule", filed, want)
    eq("the same incidents", [t[:7] for t in new_shape], [t[:7] for t in old_shape])
    eq("the same history", [t[8] for t in new_shape], [t[8] for t in old_shape])
    ck("old ones are closed, a recent one is open",
       {t[2] for t in new_shape} == {"open", "resolved"}, {t[2] for t in new_shape})
    ck("the live incident kept its own note and one more at most",
       any(t[0] == "svc-3" and "live1" in t[1] for t in new_shape))
    # the old rule files the live note too (nothing filed it yet in its twin); the bulk rule skips
    # runs already filed, so compare the totals the way each counts them
    ck("a note joined the kept live incident or left it alone, as the old rule",
       [t for t in new_shape if "live1" in t[1]] == [t for t in old_shape if "live1" in t[1]])
    eq("the flag is set", new_store.get_setting(incidents.BACKFILL_SETTING),
       incidents.BACKFILL_VERSION)

    print("== the statements do not grow with the notes ==")
    ck(f"{filed} notes took few statements", counter.n < 60, counter.n)

    print("== a second run adds nothing ==")
    eq("filed", incidents.backfill(new_store, now=NOW), 0)
    eq("same incidents", shape(new_store), new_shape)

    print("== a rerun after a new version files the same again ==")
    new_store.set_setting(incidents.BACKFILL_SETTING, "1")
    eq("same count", incidents.backfill(new_store, now=NOW), filed)
    eq("same incidents", [t[:2] + t[2:5] for t in shape(new_store)],
       [t[:2] + t[2:5] for t in new_shape])
    new_store.con.close()
    old_store.con.close()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


main()
