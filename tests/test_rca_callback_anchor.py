"""The write-back's key names the firing the run was woken by (TR-294).

Run: .venv/bin/python tests/test_rca_callback_anchor.py   (no external services needed)

The key used to be resolved after the model loop, from the newest event in the window at that
moment. A run takes ~40s, so any firing that arrived for the same entity meanwhile became the
reported key — and Rius attributes reports by that key, so the finding landed on an alert the
agent had never looked at while the alert it did investigate showed nothing.

The important test here is the one that drives `_run_traced` with newer events arriving DURING
the loop: a unit test on `_callback_anchor` alone would still pass if the call moved back after
the loop, which is exactly the bug.
"""
import asyncio
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tares.builtin_agents as ba
from tares.envelope import now_utc

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


class Condition:
    window = "5m"


class Trigger:
    name = "rius_alert_fired"
    view = "rius_alerts_view"
    condition = Condition()
    cooldown_seconds = 300.0


class View:
    name = "rius_alerts_view"
    sources = ["rius_alerts"]
    filters = None


class Runtime:
    class catalog:
        triggers = [Trigger()]
        views = [View()]


class Store:
    """Only the methods the run path touches. `events` is a mutable list of (age_s, labels)."""

    def __init__(self, events, labels_as_json=False):
        self.events = list(events)
        self.labels_as_json = labels_as_json
        self.finished = []
        self.delivery = None

    def read_view_window(self, sources, key, since, cap=12, filters=None, where=None,
                         include_payload=False):
        rows = []
        for age, labels in self.events:
            at = now_utc() - timedelta(seconds=age)
            if at < since:
                continue
            rows.append((at, sources[0], "text",
                         json.dumps(labels) if self.labels_as_json else labels))
        return sorted(rows, key=lambda r: r[0])[-cap:]

    # the run path's bookkeeping
    def agent_runs_today(self, name, exclude_run_id=None):
        return 0

    def agent_cost_total(self, name):
        return 0.0

    def finish_agent_run(self, run_id, status, **kw):
        self.finished.append((run_id, status))

    def set_run_delivery(self, run_id, delivery, error=None):
        self.delivery = (delivery, error)

    def record_run_usage(self, *a, **k):
        pass

    def record_model_usage(self, *a, **k):
        pass


class Obs:
    def set_output(self, *a):
        pass

    def set_attribute(self, *a):
        pass


AGENT = {"name": "rius_rca_agent", "prompt": "investigate", "webhook_key_label": "delivery_id",
         "webhook_url": "https://ingest.rius.invalid/v1/rca/reports"}


def runner(store):
    """An AgentRunner without __init__: it reaps stale runs and wants a real store, and nothing
    under test here touches anything but `store` and `runtime`."""
    r = object.__new__(ba.AgentRunner)
    r.store = store
    r.runtime = Runtime()
    return r


def anchor(store, agent=AGENT):
    return runner(store)._callback_anchor(agent, "rius_alert_fired", "billing-svc")


async def main():
    # The module resolves these from the store; the run path needs them non-empty to proceed.
    ba.resolve_anthropic_headers = lambda store: ({"x-api-key": "test"}, "test")
    ba.resolve_api_base = lambda store: ("https://api.invalid", "test")

    print("== THE REGRESSION: newer firings during the run must not move the key ==")
    # One firing when the run starts; three more land while the model loop is running, exactly as
    # a burst of alerts for one service does.
    store = Store([(1, {"service": "billing-svc", "delivery_id": "d-woke",
                        "alert_id": "a-woke"})])
    r = runner(store)
    posted = {}

    async def fake_loop(*a, **k):
        # arrive mid-run, newest last — this is the state the old code read from
        store.events.append((0.5, {"delivery_id": "d-later-1", "alert_id": "a-later-1"}))
        store.events.append((0.2, {"delivery_id": "d-newest", "alert_id": "a-newest"}))
        return "the finding", 2, 3, [], False, None

    async def fake_record(*a, **k):
        pass

    async def fake_webhook(agent, body, attempts=3):
        posted.update(body)
        return "ok", None

    r._loop, r._record, r._webhook = fake_loop, fake_record, fake_webhook
    status, err = await r._run_traced(AGENT, "rius_alert_fired", "billing-svc", "payload",
                                      "run_1", "dispatch_1", None, Obs())
    check("the run concluded", (status, err) == ("ok", None), f"{status} {err}")
    check("key is the firing that woke the run", posted.get("key") == "d-woke",
          str(posted.get("key")))
    check("NOT the newest firing at write-back time (the old bug)",
          posted.get("key") != "d-newest", str(posted.get("key")))
    check("labels are the waking firing's",
          (posted.get("labels") or {}).get("alert_id") == "a-woke",
          str(posted.get("labels")))
    # Proves the test is not vacuous: the later firings really are in the store, so resolving
    # after the loop WOULD have returned d-newest.
    check("newer firings were in fact present by write-back time",
          anchor(store)[0] == "d-newest", str(anchor(store)[0]))
    check("delivery recorded on the run", store.delivery == ("ok", None), str(store.delivery))

    print("== the anchor itself ==")
    store = Store([(9, {"delivery_id": "old"}), (2, {"delivery_id": "new", "alert_id": "a2"})])
    key, labels = anchor(store)
    check("newest firing in the window at call time", key == "new", key)
    check("its labels come back", labels.get("alert_id") == "a2", str(labels))

    print("== labels arriving as a JSON string ==")
    key, labels = anchor(Store([(2, {"delivery_id": "j1", "alert_id": "aj"})],
                               labels_as_json=True))
    check("json labels parsed", (key, labels.get("alert_id")) == ("j1", "aj"), f"{key} {labels}")

    print("== fallbacks: never drop the field ==")
    check("no label named -> the entity, no labels",
          anchor(Store([(1, {"delivery_id": "d"})]), agent={"name": "a"}) == ("billing-svc", {}))
    check("label on no event -> the entity",
          anchor(Store([(1, {"service": "billing-svc"})]))[0] == "billing-svc")
    check("blank label value -> the entity",
          anchor(Store([(1, {"delivery_id": ""})]))[0] == "billing-svc")
    check("no events -> the entity, no labels", anchor(Store([])) == ("billing-svc", {}))

    r = runner(Store([(1, {"delivery_id": "d"})]))
    r.runtime = Runtime()

    class NoViews:
        triggers = [Trigger()]
        views = []
    r.runtime.catalog = NoViews
    check("a trigger whose view is gone -> the entity",
          r._callback_anchor(AGENT, "rius_alert_fired", "billing-svc") == ("billing-svc", {}))

    class Boom(Store):
        def read_view_window(self, *a, **k):
            raise RuntimeError("duckdb is unhappy")
    check("a store that raises -> the entity, write-back preserved",
          anchor(Boom([])) == ("billing-svc", {}))

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
