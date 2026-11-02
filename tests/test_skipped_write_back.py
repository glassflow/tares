"""A run a trigger dispatched and the cell refused before it started is written back (RIUS-1103).

Rius files findings by the firing's delivery id and waits for one. When the daily cap, the agent's
budget or a run already in flight for the same key stopped the run, nothing came back and the
firing read "queued" for good. The write-back webhook now gets a `skipped` event with a reason,
under the key a finding would carry. A rerun, a bootstrap run or a hand-off answers for no firing
of its own and sends nothing.

Run: python tests/test_skipped_write_back.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tares.builtin_agents as ba

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


class Store:
    """What the refusal paths touch."""

    def __init__(self, runs_today=0, spent=0.0):
        self.runs_today, self.spent = runs_today, spent
        self.finished, self.run_delivery, self.deliveries = None, {}, []

    def get_setting(self, k):
        return None

    def agent_runs_today(self, *a, **k):
        return self.runs_today

    def agent_cap_frees_at(self, *a, **k):
        return None

    def agent_cost_total(self, *a):
        return self.spent

    def finish_agent_run(self, run_id, status, **kw):
        self.finished = (status, kw.get("error"))

    def set_run_delivery(self, run_id, delivery, error=None):
        self.run_delivery[run_id] = (delivery, error)

    def update_delivery(self, dispatch_id, subscription_id, ok, note):
        self.deliveries.append((dispatch_id, ok, note))

    def add_run_projects(self, *a):
        pass

    def get_catalog_agent(self, name, project=None):
        return dict(AGENT) if name == AGENT["name"] else None

    def log_delivery(self, dispatch_id, subscription_id, url, ok, note=None):
        pass


class Obs:
    def set_attribute(self, *a):
        pass


AGENT = {"name": "rius-rca", "prompt": "p", "webhook_url": "https://rius.example/v1/rca/reports",
         "webhook_token": "t"}


def runner(store):
    r = object.__new__(ba.AgentRunner)
    r.store = store
    r.posted = []
    r._tasks = set()

    async def webhook(agent, body, attempts=3):
        r.posted.append(body)
        return "ok", None

    r._webhook = webhook
    return r


async def refused(store, agent=AGENT, dispatch_id="dsp_1", handoff=False):
    ba.resolve_for_agent = lambda store, agent: (object(), "test", "anthropic", "")
    r = runner(store)
    status, err = await r._run_traced(agent, "alert", "checkout", "", "run_1", dispatch_id, None,
                                      Obs(), ("dlv_42", {"service": "checkout"}),
                                      **({"handoff": True} if handoff else {}))
    return r, status, err


async def caps():
    print("== the daily cap ==")
    store = Store(runs_today=50)
    r, status, err = await refused(store)
    check("the run is capped as before", status == "capped" and "50 runs" in (err or ""), f"{status} {err}")
    check("one skipped write-back", len(r.posted) == 1 and r.posted[0]["event"] == "skipped", r.posted)
    body = r.posted[0] if r.posted else {}
    check("reason daily_cap, with the message", body.get("reason") == "daily_cap"
          and "50 runs" in body.get("message", ""), body)
    check("under the firing's key and labels, as a finding would be",
          body.get("key") == "dlv_42" and body.get("labels") == {"service": "checkout"}, body)
    check("naming the run and the dispatch", body.get("run_id") == "run_1"
          and body.get("dispatch_id") == "dsp_1", body)
    check("the write-back's delivery is recorded on the run",
          store.run_delivery.get("run_1") == ("ok", None), store.run_delivery)

    print("== the budget ==")
    r, status, err = await refused(Store(spent=5.0), agent={**AGENT, "budget_usd": 5})
    check("the run is capped as before", status == "capped" and "budget" in (err or ""), f"{status} {err}")
    check("reason budget_exhausted", [b.get("reason") for b in r.posted] == ["budget_exhausted"], r.posted)

    print("== runs that answer for no firing ==")
    r, status, _ = await refused(Store(runs_today=50), dispatch_id=None)
    check("a rerun or bootstrap run (no dispatch) writes nothing back", status == "capped"
          and r.posted == [], r.posted)
    r, status, _ = await refused(Store(runs_today=50), handoff=True)
    check("a hand-off writes nothing back", status == "capped" and r.posted == [], r.posted)
    r, status, _ = await refused(Store(runs_today=50), agent={**AGENT, "webhook_url": ""})
    check("an agent with no write-back webhook sends nothing", status == "capped"
          and r.posted == [], r.posted)

    print("== a webhook that fails ==")
    store = Store(runs_today=50)
    r = runner(store)

    async def broken(agent, body, attempts=3):
        raise RuntimeError("boom")

    r._webhook = broken
    status, err = await r._run_traced(AGENT, "alert", "checkout", "", "run_1", "dsp_1", None, Obs(),
                                      ("dlv_42", {}))
    check("the refusal still returns capped", status == "capped", f"{status} {err}")


async def in_flight():
    print("== a firing that arrives while a run for its key is going (deliver) ==")
    # The dedupe almost every duplicate meets is deliver's own, before any task starts.
    store = Store()
    r = runner(store)
    r._inflight = {("rius-rca", "checkout")}
    r._inflight_run = {("rius-rca", "checkout"): "run_going"}
    r._callback_anchor = lambda agent, trigger_name, key: ("dlv_44", {"service": "checkout"})
    started = r.deliver("rius-rca", "sub_1", "alert", "checkout", "", "dsp_3")
    await asyncio.gather(*list(r._tasks))
    check("no run starts", started is None, started)
    check("the delivery is deduped as before",
          store.deliveries == [("dsp_3", True, "deduped (already running)")], store.deliveries)
    check("one skipped write-back, reason in_flight, under this firing's key",
          [(b.get("reason"), b.get("key"), b.get("in_flight_run_id")) for b in r.posted]
          == [("in_flight", "dlv_44", "run_going")], r.posted)

    print("== the same, met in _guarded (a duplicate that slips in between) ==")
    store = Store()
    r = runner(store)
    r._inflight = {("rius-rca", "checkout")}
    r._inflight_run = {("rius-rca", "checkout"): "run_going"}
    r._callback_anchor = lambda agent, trigger_name, key: ("dlv_43", {"service": "checkout"})
    await r._guarded(AGENT, "sub_1", "alert", "checkout", "", "dsp_2")
    await asyncio.gather(*list(r._tasks))
    check("the delivery is deduped as before", store.deliveries == [("dsp_2", True, "deduped (already running)")],
          store.deliveries)
    check("one skipped write-back, reason in_flight", [b.get("reason") for b in r.posted] == ["in_flight"],
          r.posted)
    body = r.posted[0] if r.posted else {}
    check("under this firing's key, naming the run in flight", body.get("key") == "dlv_43"
          and body.get("dispatch_id") == "dsp_2" and body.get("in_flight_run_id") == "run_going", body)
    check("the run in flight keeps its own delivery record", store.run_delivery == {}, store.run_delivery)


async def main():
    await caps()
    await in_flight()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
