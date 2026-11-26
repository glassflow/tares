"""The saved entity counts of a filtered read answer what a live scan answers (TR-1716).

GET /api/entities leaves out the rows of the private projects a caller is not on, and of the deleted
private ones. The store saves what such a read counted (tares/saved_counts.py). Every caller's answer
from the saved counts is compared here with the same read counted afresh: after a delete, after
appended rows that stay and rows that are left out, after a later line joins an older session to the
deleted project, after its name is taken again and given up, after a source is purged, replaced and
relabelled, after a restart, and past the cardinality cap.

Run: .venv/bin/python tests/test_saved_entity_counts.py
Postgres: TARES_TEST_PG=postgresql://... PYTHONPATH=tests/pg_harness .venv/bin/python tests/test_saved_entity_counts.py
"""
import asyncio
import contextlib
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = "root-token-saved-entity-counts"
HOME = tempfile.mkdtemp(prefix="tares-saved-entity-counts-")
os.environ.update({"TARES_DB": os.path.join(HOME, "import.duckdb"),
                   "TARES_CATALOG": os.path.join(HOME, "catalog.yaml"),
                   "TARES_OTLP_GRPC_PORT": "off", "TARES_AUTH_TOKEN": ROOT,
                   "TARES_EDITION": "factory"})

import httpx

from tares import daemon
from tares import store as store_module
from tares.envelope import Envelope, now_utc
from tares.saved_counts import UNSAVED, SavedCounts

P = F = 0


def ck(label, cond, detail: object = ""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {str(detail)[:400]}"))


VAULT, QUARTZ, ONYX, AGAIN = "uc_a_vault", "uc_b_quartz", "uc_c_onyx", "uc_d_again"
NAME = "Zephyrvault"
# root: the cell's token; bo: on no project; kim: owns a crew; mo: on Zephyrvault and Quartz;
# ann: on Onyx. After the delete mo and ann each leave out one live and one deleted project.
CALLERS = ("root", "bo", "kim", "mo", "ann")
EVERYBODY = set(CALLERS)


@contextlib.asynccontextmanager
async def cell(db: str):
    """(store, client, the labels a live scan was run for) on a cell started over `db`."""
    daemon.DB_PATH = db
    app = daemon.make_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     timeout=120) as cx:
            store = app.state.store
            scans: list = []
            real = store._list_entities_scan

            def scan(*args, **kw):
                scans.append(args[0])
                return real(*args, **kw)

            store._list_entities_scan = scan
            yield store, cx, scans


def event(source, key, service, labels=None, payload=None, at=None):
    return Envelope(source, "event_stream", key, "evt", f"{service} line",
                    at or now_utc() - timedelta(minutes=5), payload or {},
                    {"service": service, **(labels or {})})


def line(sid, kind, **more):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return {"type": kind, "sessionId": sid, "cwd": "/w/crew", "timestamp": stamp[:-3] + "Z", **more}


async def ingest(cx, headers, lines):
    r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                      headers={**headers, "Content-Type": "application/x-ndjson"})
    assert r.status_code < 300, r.text
    await asyncio.sleep(0.5)


async def entities(cx, headers) -> dict:
    """{label: its (value, events, last ingest), every one of them} as the caller reads them."""
    r = await cx.get("/api/entities", params={"limit": 50}, headers=headers)
    assert r.status_code == 200, r.text
    got = {e["label"]: sorted((str(v["value"]), v["events"], v["last_ingest"]) for v in e["values"])
           for e in r.json()["labels"]}
    assert all(len(values) < 50 for values in got.values()), got   # ties never decide the answer
    return got


async def compare(world, store, cx, scans, stage, seen=None, unscanned=False):
    """Each caller's read, against the same read with nothing saved. `seen` is {service: who reads
    it}; `unscanned`: the read has to come from the saved counts."""
    for who in CALLERS:
        del scans[:]
        saved = await entities(cx, world[who])
        scanned = list(scans)
        kept, store._saved_counts = store._saved_counts, SavedCounts()
        try:
            fresh = await entities(cx, world[who])
        finally:
            store._saved_counts = kept
        ck(f"{stage}, {who}: the answer is the live scan's", saved == fresh,
           {k: (saved.get(k), fresh.get(k)) for k in {*saved, *fresh} if saved.get(k) != fresh.get(k)})
        if unscanned:
            ck(f"{stage}, {who}: the events are not scanned", not scanned, scanned)
        if seen is not None:
            got = {v for v, _, _ in saved.get("service", [])}
            want = {service for service, readers in seen.items() if who in readers}
            ck(f"{stage}, {who}: the services read are the expected ones", got == want,
               f"extra {sorted(got - want)}, missing {sorted(want - got)}")


async def main():
    db = os.path.join(HOME, "cell.duckdb")
    root = {"Authorization": f"Bearer {ROOT}"}
    world = {"root": root}
    labels = [{"name": "service", "field": "service", "primary": True},
              {"name": "tares_project", "field": "tares_project"}]
    async with cell(db) as (store, cx, scans):
        for uid, name in ((VAULT, NAME), (QUARTZ, "Quartz"), (ONYX, "Onyx")):
            store.create_project(uid, "custom", name, {}, goal="A project with events")
            store.set_project_scope(uid, "private")
        for source in ({"name": "claude_code", "connector": "claude_code", "poll": "10s",
                        "config": {"push": True}},
                       {"name": "evts", "connector": "webhook", "config": {"labels": labels}},
                       {"name": "findings", "connector": "finding", "config": {}},
                       {"name": "solo", "connector": "webhook", "config": {"labels": labels},
                        "project": VAULT}):
            r = await cx.post("/api/sources", json=source, headers=root)
            assert r.status_code < 300, r.text
        for who in CALLERS[1:]:
            person = store.create_person(who.capitalize(), f"{who}@example.com")["id"]
            token = f"nvf_{who}_{who}-secret-for-the-test"
            store.insert_api_key(who, who, f"nvf_{who}", hashlib.sha256(token.encode()).hexdigest(),
                                 ["read", "ingest"], person_id=person)
            world[who] = {"Authorization": f"Bearer {token}"}
            for uid in {"mo": (VAULT, QUARTZ), "ann": (ONYX,)}.get(who, ()):
                store.add_project_member(uid, person)
        await ingest(cx, world["kim"], [
            line("own-0001", "session_station", station="shop-build-m1", role="builder",
                 factory_station="shop-build-m1", factory_crew="crew", crew="crew",
                 tares_project=VAULT)])
        for uid, sid in ((VAULT, "sess-vault"), (QUARTZ, "sess-quartz"), (ONYX, "sess-onyx")):
            store.link_session(uid, sid, None)
        store.append([
            event("evts", "shop", "svc-shop"),
            event("evts", "shop", "svc-shop"),
            event("evts", "old-0001", "svc-old-session"),
            event("evts", "shop", "svc-quartz", {"tares_project": QUARTZ}),
            event("evts", "sess-onyx", "svc-onyx"),
            event("evts", "shop", "svc-vault-id", {"tares_project": VAULT}),
            event("evts", "sess-vault", "svc-vault-session"),
            event("solo", "shop", "svc-vault-source"),
        ])

        print("== before the delete ==")
        await compare(world, store, cx, scans, "private")
        r = await cx.delete(f"/api/projects/{VAULT}", headers=root)
        assert r.status_code < 300, r.text

        print("== after the delete ==")
        seen = {"svc-shop": EVERYBODY, "svc-old-session": EVERYBODY,
                "svc-quartz": {"root", "mo"}, "svc-onyx": {"root", "ann"}}
        await compare(world, store, cx, scans, "deleted", seen)
        await compare(world, store, cx, scans, "deleted, read again", seen, unscanned=True)

        print("== rows appended, kept and left out ==")
        naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        old = datetime.now(timezone.utc) - timedelta(days=2)
        store.append([
            event("evts", "shop", "svc-shop"),
            event("evts", "shop", "svc-new"),
            event("evts", None, "svc-no-key"),
            event("evts", "shop", "svc-naive", at=naive),
            event("evts", "shop", "svc-quartz", {"tares_project": "Quartz"}),
            event("evts", "sess-quartz", "svc-quartz"),
            event("evts", "shop", "svc-onyx", {"tares_project": ONYX}),
            event("evts", "shop", "svc-vault-id", {"tares_project": VAULT}),
            event("evts", "shop", "svc-vault-name", {"tares_project": NAME}),
            event("evts", "sess-vault", "svc-vault-session"),
            event("findings", "shop", "svc-vault-payload", payload={"project": VAULT}),
            event("findings", "shop", "svc-finding"),
            event("solo", "shop", "svc-solo-old", at=old),
            event("solo", "shop", "svc-solo-old", at=old.replace(tzinfo=None)),
            event("solo", "shop", "svc-solo-new", at=now_utc()),
        ])
        seen.update({s: EVERYBODY for s in ("svc-new", "svc-no-key", "svc-naive", "svc-finding",
                                            "svc-solo-new")})
        await compare(world, store, cx, scans, "appended", seen, unscanned=True)

        print("== a later line joins an older session to the deleted project ==")
        await ingest(cx, world["kim"], [line("old-0001", "session_project", tares_project=VAULT)])
        seen["svc-old-session"] = set()
        await compare(world, store, cx, scans, "joined later", seen)
        store.append([event("evts", "old-0001", "svc-old-session"),
                      event("evts", "shop", "svc-shop")])
        await compare(world, store, cx, scans, "joined later, appended", seen, unscanned=True)

        print("== the name is taken again, then given up ==")
        store.create_project(AGAIN, "custom", NAME, {}, goal="The name, taken again")
        await asyncio.sleep(0.1)
        await compare(world, store, cx, scans, "name taken", seen)
        store.append([event("evts", "shop", "svc-reused", {"tares_project": NAME}, at=now_utc()),
                      event("evts", "shop", "svc-vault-name", {"tares_project": NAME}, at=old)])
        seen["svc-reused"] = EVERYBODY
        await compare(world, store, cx, scans, "name taken, appended", seen, unscanned=True)
        store.update_project(AGAIN, name="Elsewhere")
        seen["svc-reused"] = set()
        # the filter is the one from before the name was taken, and its counts were kept up since
        await compare(world, store, cx, scans, "name given up", seen, unscanned=True)

        print("== a source purged, then replaced ==")
        store.purge_events("evts")
        seen = {"svc-finding": EVERYBODY, "svc-solo-new": EVERYBODY}
        await compare(world, store, cx, scans, "purged", seen)
        store.replace_source_events("solo", [
            event("solo", "shop", "svc-solo-replaced", at=now_utc()),
            event("solo", "shop", "svc-vault-id", {"tares_project": VAULT}, at=now_utc())])
        seen = {"svc-finding": EVERYBODY, "svc-solo-replaced": EVERYBODY}
        await compare(world, store, cx, scans, "replaced", seen)

        print("== a source's labels computed again ==")
        store.backfill_labels("findings", [{"name": "service", "const": "svc-relabelled"}])
        seen = {"svc-relabelled": EVERYBODY, "svc-solo-replaced": EVERYBODY}
        await compare(world, store, cx, scans, "relabelled", seen)

    async with cell(db) as (store, cx, scans):
        print("== after a restart ==")
        await compare(world, store, cx, scans, "restarted", seen)
        store.append([event("evts", "shop", "svc-after-restart"),
                      event("evts", "shop", "svc-quartz", {"tares_project": QUARTZ}),
                      event("evts", "shop", "svc-vault-id", {"tares_project": VAULT})])
        seen.update({"svc-after-restart": EVERYBODY, "svc-quartz": {"root", "mo"}})
        await compare(world, store, cx, scans, "restarted, appended", seen, unscanned=True)

        print("== past the cardinality cap ==")
        cap = store_module._ENTITY_CARDINALITY_CAP
        store_module._ENTITY_CARDINALITY_CAP = 12
        try:
            many = [f"svc-many-{n:02d}" for n in range(20)]
            store.append([event("evts", "shop", service) for service in many])
            seen.update({service: EVERYBODY for service in many})
            await compare(world, store, cx, scans, "past the cap", seen)
            del scans[:]
            await entities(cx, world["bo"])
            ck("past the cap: the label is counted from the events", "service" in scans, scans)
        finally:
            store_module._ENTITY_CARDINALITY_CAP = cap


def bounded_upkeep():
    print("== what each batch pays for ==")
    saved, spot = SavedCounts(), (None, "service")
    filters = [(name, ("v",) * 4000) for name in ("a", "b", "c")]
    for filt in filters:
        saved.put(filt, spot, {})
    kept = [saved.get(filt, spot) is not UNSAVED for filt in filters]
    ck("the filters kept stay under the bound on their parameters", kept == [False, True, True], kept)
    large = ("d", ("v",) * 40_000)
    saved.put(large, spot, {})
    ck("one filter over the bound is still kept, alone",
       saved.get(large, spot) == {} and saved.get(filters[2], spot) is UNSAVED)


asyncio.run(main())
bounded_upkeep()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
