"""The write-back's fan-out: one investigation, one report per firing it covers.

Run: .venv/bin/python tests/test_rca_fanout.py   (no external services needed)

A trigger fires on an aggregate over a window, not on one event, so a burst of firings inside one
cooldown is a single investigation. Reporting one `webhook_key_label` value meant the rest of the
burst got no report at all, and the one report was keyed to an arbitrary member of it — the newest
event, while the agent had usually written up the oldest. These assert the whole set is reported,
that the lookback cannot reach into an earlier run's firings, and that the fallbacks still hold.
"""
import asyncio
import json
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tares.builtin_agents import CALLBACK_KEY_CAP, AgentRunner
from tares.envelope import now_utc

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


class Condition:
    def __init__(self, window):
        self.window = window


class Trigger:
    def __init__(self, window="5m", cooldown=300.0):
        self.name = "rius_alert_fired"
        self.view = "rius_alerts_view"
        self.condition = Condition(window)
        self.cooldown_seconds = cooldown


class View:
    name = "rius_alerts_view"
    sources = ["rius_alerts"]
    filters = None


class Catalog:
    def __init__(self, trig):
        self.triggers = [trig]
        self.views = [View()]


class Runtime:
    def __init__(self, catalog):
        self.catalog = catalog


class Store:
    """Only what _callback_keys touches. `events` are (age_seconds, labels), newest age smallest."""

    def __init__(self, events, labels_as_json=False):
        self.events = events
        self.labels_as_json = labels_as_json
        self.since_asked = None
        self.cap_asked = None

    def read_view_window(self, sources, key, since, cap=12, filters=None, where=None,
                         include_payload=False):
        self.since_asked, self.cap_asked = since, cap
        rows = []
        for age, labels in self.events:
            at = now_utc() - timedelta(seconds=age)
            if at < since:
                continue
            rows.append((at, sources[0], "text",
                         json.dumps(labels) if self.labels_as_json else labels))
        # The real store returns each source's most-recent `cap` events.
        return sorted(rows, key=lambda r: r[0])[-cap:]


def runner(store, trig=None):
    """An AgentRunner without its __init__: that reaps stale runs and needs a real store, and
    neither of the two methods under test touches anything else on the object."""
    r = object.__new__(AgentRunner)
    r.store = store
    r.runtime = Runtime(Catalog(trig or Trigger()))
    return r


AGENT = {"name": "rius_rca_agent", "webhook_key_label": "delivery_id",
         "webhook_url": "https://ingest.rius.invalid/v1/rca/reports"}


def keys_for(store, agent=AGENT, trig=None):
    return runner(store, trig)._callback_keys(agent, "rius_alert_fired", "billing-svc")


async def main():
    print("== every firing in the window is reported, oldest first ==")
    # The burst that produced the bug: 6 firings 2s apart, one run woken by the oldest.
    burst = [(12 - i, {"service": "billing-svc", "delivery_id": f"d{i}"}) for i in range(6)]
    got = keys_for(Store(burst))
    check("all six delivery ids reported", got == [f"d{i}" for i in range(6)], str(got))
    check("not just the newest (the old bug)", got != ["d5"], str(got))
    check("anchored on the oldest, which the agent writes up", got[0] == "d0", str(got))

    print("== duplicates and blanks ==")
    dupes = [(9, {"delivery_id": "a"}), (6, {"delivery_id": "a"}), (3, {"delivery_id": "b"}),
             (2, {"delivery_id": ""}), (1, {"service": "billing-svc"})]
    got = keys_for(Store(dupes))
    check("each value once, blank and missing skipped", got == ["a", "b"], str(got))

    print("== labels arriving as a JSON string ==")
    got = keys_for(Store([(5, {"delivery_id": "j1"}), (4, {"delivery_id": "j2"})],
                         labels_as_json=True))
    check("json labels parsed", got == ["j1", "j2"], str(got))

    print("== the lookback cannot reach an earlier run's firings ==")
    # cooldown 5m, so anything older than 5m belongs to a firing an earlier run already answered.
    # Reporting it would overwrite that run's report for the same delivery.
    store = Store([(600, {"delivery_id": "old"}), (30, {"delivery_id": "new"})])
    got = keys_for(store)
    check("older than the cooldown is excluded", got == ["new"], str(got))
    asked = (now_utc() - store.since_asked).total_seconds()
    check("lookback is the cooldown, not a wider floor", 290 <= asked <= 310, f"{asked:.0f}s")
    # A cooldown shorter than the window is the binding constraint, and vice versa.
    store = Store([(30, {"delivery_id": "n"})], )
    keys_for(store, trig=Trigger(window="5m", cooldown=60.0))
    asked = (now_utc() - store.since_asked).total_seconds()
    check("min(window, cooldown) wins when cooldown is shorter", 55 <= asked <= 65, f"{asked:.0f}s")
    store = Store([(30, {"delivery_id": "n"})])
    keys_for(store, trig=Trigger(window="1m", cooldown=300.0))
    asked = (now_utc() - store.since_asked).total_seconds()
    check("min(window, cooldown) wins when window is shorter", 55 <= asked <= 65, f"{asked:.0f}s")
    store = Store([(30, {"delivery_id": "n"})])
    keys_for(store, trig=Trigger(window="2m", cooldown=0.0))
    asked = (now_utc() - store.since_asked).total_seconds()
    check("no cooldown falls back to the window", 115 <= asked <= 125, f"{asked:.0f}s")

    print("== a burst larger than the store's default cap ==")
    big = [(200 - i, {"delivery_id": f"b{i}"}) for i in range(40)]
    store = Store(big)
    got = keys_for(store)
    check("more than the default cap of 12 is reported", len(got) == 40, str(len(got)))
    check("the cap asked for is the callback cap", store.cap_asked == CALLBACK_KEY_CAP,
          str(store.cap_asked))

    print("== fallbacks: never drop the field ==")
    check("no label named -> the entity",
          keys_for(Store(burst), agent={"name": "a"}) == ["billing-svc"])
    check("label on no event -> the entity",
          keys_for(Store([(5, {"service": "billing-svc"})])) == ["billing-svc"])
    check("no events at all -> the entity", keys_for(Store([])) == ["billing-svc"])
    r = runner(Store(burst))
    r.runtime = Runtime(Catalog(Trigger()))
    r.runtime.catalog.views = []          # a trigger naming a view that is gone
    check("missing view -> the entity",
          r._callback_keys(AGENT, "rius_alert_fired", "billing-svc") == ["billing-svc"])

    print("== the store raising must not lose the write-back ==")
    class Boom(Store):
        def read_view_window(self, *a, **k):
            raise RuntimeError("duckdb is unhappy")
    check("lookup failure -> the entity", keys_for(Boom([])) == ["billing-svc"])

    print("== _deliver_finding posts once per key ==")
    posts = []

    async def fake_webhook(agent, body, attempts=3):
        posts.append(body)
        return "ok", None

    r = runner(Store(burst))
    r._webhook = fake_webhook
    delivery, err = await r._deliver_finding(AGENT, "rius_alert_fired", "billing-svc",
                                             {"event": "finding", "run_id": "run_1"})
    check("one post per firing", len(posts) == 6, str(len(posts)))
    check("each carries its own key", [p["key"] for p in posts] == [f"d{i}" for i in range(6)],
          str([p["key"] for p in posts]))
    check("all share one run id", {p["run_id"] for p in posts} == {"run_1"})
    check("the body is otherwise identical",
          all(p["event"] == "finding" for p in posts))
    check("all ok -> ok", (delivery, err) == ("ok", None), f"{delivery} {err}")

    print("== one key failing does not stop or mask the others ==")
    posts.clear()
    calls = {"n": 0}

    async def flaky(agent, body, attempts=3):
        calls["n"] += 1
        posts.append(body)
        if calls["n"] == 2:
            return "http 400", "bad request"
        if calls["n"] == 4:
            return "failed", "connection reset"
        return "ok", None

    r = runner(Store(burst))
    r._webhook = flaky
    delivery, err = await r._deliver_finding(AGENT, "rius_alert_fired", "billing-svc", {})
    check("every key still attempted", len(posts) == 6, str(len(posts)))
    check("the FIRST failure is what the run reports", (delivery, err) == ("http 400", "bad request"),
          f"{delivery} {err}")

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
