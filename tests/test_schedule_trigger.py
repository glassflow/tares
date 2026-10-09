"""Schedule triggers (TR-320): a trigger with `every` fires once per interval for all its sources,
handing over a summary of the window; condition triggers are unchanged.

Runs the clock's `tick` directly against a real store and catalog, with a dispatcher that records
what it was asked to deliver.
"""
import asyncio
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tares import schedule
from tares.config import CatalogError, catalog_from_db, validate_trigger_dict
from tares.envelope import Envelope, now_utc
from tares.store import Store
from tares.triggers import eval_triggers

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


class Dispatcher:
    def __init__(self):
        self.fired = []

    async def fire(self, trigger, key, payload):
        self.fired.append((trigger.name, key, payload))


def ev(minutes_ago, **labels):
    return Envelope(source="logs", source_type="application_log",
                    key_value=labels.get("service", "x"), event_type="log",
                    text=f"GET / {labels.get('code', '200')}",
                    event_time=now_utc() - timedelta(minutes=minutes_ago), payload={},
                    labels={k: str(v) for k, v in labels.items()})


def rejects(t):
    try:
        validate_trigger_dict(t, {"logs"})
        return False
    except CatalogError:
        return True


async def main():
    print("== validation ==")
    ok = {"name": "watch", "sources": ["logs"], "condition": {"every": "10m", "summary_by": ["service"]}}
    try:
        validate_trigger_dict(ok, {"logs"})
        check("a schedule condition validates without aggregate or predicate", True)
    except CatalogError as e:
        check("a schedule condition validates without aggregate or predicate", False, str(e))
    check("every under a minute is rejected",
          rejects({"name": "w", "sources": ["logs"], "condition": {"every": "30s"}}))
    check("a bad summary label is rejected",
          rejects({"name": "w", "sources": ["logs"], "condition": {"every": "5m", "summary_by": ["a b"]}}))
    check("condition triggers still need an aggregate",
          rejects({"name": "w", "sources": ["logs"], "condition": {"predicate": "> 0", "window": "1m"}}))
    check("a trigger naming a view is rejected",
          rejects({"name": "w", "view": "logs", "condition": {"every": "10m"}}))
    check("a trigger over an unknown source is rejected",
          rejects({"name": "w", "sources": ["nope"], "condition": {"every": "10m"}}))

    store = Store(os.path.join(tempfile.mkdtemp(), "t.duckdb"))
    store.upsert_catalog_source("logs", "application_log", "webhook", "5s", {})
    store.upsert_catalog_trigger("watch", ["logs"], {"every": "10m", "summary_by": ["service", "code"]},
                                 {}, "5m", key_field="service")
    store.upsert_catalog_trigger("spike", ["logs"],
                                 {"aggregate": "count", "predicate": "> 0", "window": "5m"}, {}, "5m")
    store.append([ev(3, service="ui", code="404") for _ in range(30)]
                 + [ev(4, service="api", code="200") for _ in range(5)]
                 + [ev(15, service="ui", code="404") for _ in range(2)])
    catalog = catalog_from_db(store)
    trig = next(t for t in catalog.triggers if t.name == "watch")
    check("parsed as a schedule of 600s", trig.condition.every == 600.0
          and trig.condition.summary_by == ["service", "code"], str(trig.condition))

    print("== the clock ==")
    d = Dispatcher()
    t0 = now_utc()
    fired = await schedule.tick(store, catalog, d, now=t0)
    check("the first tick fires the schedule trigger only", fired == ["watch"], str(fired))
    name, key, payload = d.fired[0]
    check("one firing for the whole trigger, keyed by the trigger", key == "watch", key)
    check("the summary names the entity label", "values of `service`" in payload, payload[:300])
    check("the summary counts per service", "service | now | before | change" in payload
          and "ui | 30 | 2 |" in payload, payload)
    check("and per status code", "code | now | before | change" in payload and "404 | 30 | 2 |" in payload, payload)
    check("with a few recent lines", "most recent lines:" in payload and "GET / 404" in payload, payload)

    fired = await schedule.tick(store, catalog, d, now=t0 + timedelta(minutes=5))
    check("not again before the interval", fired == [], str(fired))
    fired = await schedule.tick(store, catalog, d, now=t0 + timedelta(minutes=10))
    check("again once the interval has passed", fired == ["watch"], str(fired))

    fresh = catalog_from_db(store)   # a restart: a new catalog over the same store
    fired = await schedule.tick(store, fresh, d, now=t0 + timedelta(minutes=12))
    check("a restart does not fire the tick twice", fired == [], str(fired))

    store.set_trigger_paused("watch", True)
    paused = catalog_from_db(store)
    fired = await schedule.tick(store, paused, d, now=t0 + timedelta(minutes=30))
    check("a paused schedule does not fire", fired == [], str(fired))
    store.set_trigger_paused("watch", False)
    fired = await schedule.tick(store, catalog_from_db(store), d, now=t0 + timedelta(minutes=30))
    check("resumed, it fires on the next tick", fired == ["watch"], str(fired))

    print("== a quiet window still fires ==")
    store.upsert_catalog_trigger("quiet", ["nothing"], {"every": "5m"}, {}, "5m")
    d2 = Dispatcher()
    fired = await schedule.tick(store, catalog_from_db(store), d2, now=t0)
    quiet = [p for n, _k, p in d2.fired if n == "quiet"]
    check("a trigger with no events still gets its tick", len(quiet) == 1, str(fired))
    check("and says the window was empty", bool(quiet) and "(no events in this window)" in quiet[0], quiet[0] if quiet else "")

    print("== condition triggers are unchanged ==")
    d3 = Dispatcher()
    fired = await eval_triggers(store, catalog_from_db(store), d3, affected_sources={"logs"})
    names = {n for n, _k in fired}
    check("the condition trigger fires per key on ingest", "spike" in names, str(fired))
    check("the schedule trigger never fires on ingest", "watch" not in names and "quiet" not in names, str(fired))


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
