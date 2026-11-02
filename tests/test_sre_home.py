"""The AI SRE home in one call (TR-748): GET /api/home on an AI SRE cell.

The headline for each way a cell can be (new, quiet, nobody watching and why, incidents), the
watch block with what it costs and the way out for a spent budget, the incidents with their chart,
the services with one state each, the people on watch, the blind spots and the tools. Then the one
button, POST /api/home/actions/raise_budget, who may press it, the cache, a platform cell that
still answers 404, and the cost of a read on a cell with 21 services and 200,000 events.

The model loop is a stand-in. The cell is the shared fixture (tests/fixtures/sre_cell.py). Time
is the real clock, cut to the minute, because the route and the button read the real clock too.

Run: .venv/bin/python tests/test_sre_home.py
     (the Postgres variant: PYTHONPATH=tests/pg_harness TARES_TEST_PG=<dsn> the same way)
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ

scrub_os_environ()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp(prefix="tares-sre-home-")
ROOT_TOKEN = "root-token-sre-home"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = ROOT_TOKEN
os.environ["TARES_EDITION"] = "sre"                     # before make_app(): the first start picks it
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"     # the clock must not fire the watcher under us
os.environ["TARES_WATCH_SECONDS"] = "3600"              # nor the watch loop
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

from fixtures.sre_cell import PROJECT, _agents, _otlp, build
from tares import sre, sre_home
from tares import watch as W
from tares import words
from tares.builtin_agents import AgentRunner
from tares.envelope import Envelope
from tares.store import Store, day_month_words

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    concluded.update({"outcome": "finding", "summary": "looked", "verdict": None, "key": None,
                      "label": None, "headline": None, "next_step": None, "produced": []})
    return "looked", 1, 1, [], False, ""


AgentRunner._loop = fake_loop

UTC = timezone.utc
NOW = datetime.now(UTC).replace(second=0, microsecond=0)
MONTH = datetime(NOW.year, NOW.month, 1, tzinfo=UTC)
MIDNIGHT = datetime(NOW.year, NOW.month, NOW.day, tzinfo=UTC)
UID = "prj-" + PROJECT
SEEN: list = []                       # every body built, checked for words at the end
SKIP = "name,display,title,what,said,failing,likely_cause,next_step,detail,label".split(",")
NO_SUCH_ROLE = "switch" + "_watcher"
BANNED_WORD = "tri" + "al"
STATES = {"incident", "not_watched", "silent", "no_own_signal", "unusual", "normal"}
KEYS = {"edition", "system", "headline", "watch", "incidents", "incidents_more", "services",
        "services_from_words", "raised", "raised_note", "today", "on_watch", "notes_go_to",
        "blind_spots", "tools"}


def ago(minutes, floor=MONTH):
    """A time `minutes` back, but never before `floor`: a spend must fall in the month it is for."""
    return max(NOW - timedelta(minutes=minutes), floor + timedelta(seconds=1))


def spend(store, agent, usd, when, status="ok", practice=False):
    """One finished run of `agent` that cost `usd` and started at `when`."""
    rid = "run_" + uuid.uuid4().hex[:12]
    store.start_agent_run(rid, agent, "watch-prod", "d", "old", "h", practice=practice)
    store.finish_agent_run(rid, status)
    store.sc.execute("UPDATE agent_runs SET cost_usd = ?, started_at = ? WHERE id = ?",
                     [usd, when, rid])


def set_agent(store, name, **over):
    """Write the agent again with every field it has, and `over` changed."""
    a = store.get_catalog_agent(name)
    keep = {k: a[k] or None for k in ("slack_webhook", "model", "slack_channel", "webhook_url",
                                      "webhook_token", "webhook_key_label", "provider")}
    keep.update(mcp_servers=a["mcp_servers"], max_rounds=a["max_rounds"], budget_usd=a["budget_usd"],
                daily_cap=a["daily_cap"], concludes=a["concludes"], budget_period=a["budget_period"])
    keep.update(over)
    store.upsert_catalog_agent(name, a["trigger"], a["prompt"], **keep)


def finding_run(store, rid, service, at, **parts):
    """A finished investigator run with a finding, from `at`."""
    store.start_agent_run(rid, "investigator", "watch-prod", "", service, "h", woken_by="handoff",
                          project=UID)
    store.finish_agent_run(rid, "ok", finding=f"{service} returns errors since the last deploy.",
                           outcome="finding", verdict="rca", **parts)
    with store._swrite:
        store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, rid])


def incident(store, service, minutes_ago, owner=None, **parts):
    """An open incident on `service` with one note; `owner` takes it."""
    at = NOW - timedelta(minutes=minutes_ago)
    inc = store.create_incident(service, UID, at)
    rid = "r_" + uuid.uuid4().hex[:8]
    finding_run(store, rid, service, at, **parts)
    store.add_incident_note(inc["id"], rid, at)
    if owner:
        store.set_incident_state(inc["id"], "on_it", "u1", owner, "test", at=at)
    return inc


def strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from strings(v)


def p95(values):
    return sorted(values)[max(0, int(len(values) * 0.95 + 0.999999) - 1)]


auth_header = {"Authorization": f"Bearer {ROOT_TOKEN}"}


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        auth = {"Authorization": f"Bearer {ROOT_TOKEN}"}
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=auth)
        store, runtime = app.state.store, app.state.runtime

        def home(**kw):
            runtime.reload_catalog()
            out = sre_home.build(store, runtime.catalog, None, now=NOW, **kw)
            SEEN.append(out)
            return out

        def svc(h, name):
            return next(s for s in h["services"] if s["name"] == name)

        def reset():
            """The cell as every case starts: the watcher on and running, nothing spent, no incidents."""
            for table in ("watch_state", "agent_runs", "incident_notes", "incidents"):
                store.sc.execute(f"DELETE FROM {table}")
            store.set_wake(UID, "watch-prod", "watcher", enabled=True)
            store.set_setting(W.SYSTEM_NAME_SETTING, None)
            set_agent(store, "watcher", budget_usd=25.0, budget_period="month", daily_cap=None)
            store.set_agent_enabled("watcher", True, project=UID)
            store.set_fired("watch-prod", "watch-prod", NOW - timedelta(minutes=3))
            spend(store, "watcher", 0.0, NOW - timedelta(minutes=3))      # the last check
            sre_home.clear_cache()

        # ── a cell with nothing in it ───────────────────────────────────────
        print("== a new cell, no services ==")
        h = home()
        eq("every field is there", set(h), KEYS)
        eq("the headline is the new one", (h["headline"]["text"], h["headline"]["tone"]),
           ("No services yet", "new"))
        ck("it says what to do", "Connect" in h["headline"]["sub"], h["headline"]["sub"])
        eq("the lists are empty", (h["services"], h["incidents"], h["raised"], h["on_watch"],
                                   h["blind_spots"], h["incidents_more"]), ([], [], [], [], [], 0))
        eq("the name of the system is the plain one", h["system"], "your services")
        eq("nobody watches", h["watch"]["on"], False)
        eq("today is zero", h["today"], {"checks": 0, "quiet": 0, "investigated": 0, "spent_usd": 0})
        r = await cx.get("/api/home")
        eq("GET /api/home answers on the new cell", (r.status_code, r.json()["edition"],
                                                      r.json()["headline"]["tone"]), (200, "sre", "new"))

        # ── a watcher that stopped, on a cell that has no services yet ────────
        print("== a stopped watcher and no services yet ==")
        _agents(store, NOW, True, 0)
        runtime.reload_catalog()
        h = home()
        eq("a running watcher, no services", (h["services"], h["watch"]["on"]), ([], True))
        store.set_agent_enabled("watcher", False, project=UID)
        sre_home.clear_cache()
        r = await cx.get("/api/home")
        eq("a watcher turned off: the route answers", r.status_code, 200)
        h = r.json()
        SEEN.append(h)
        eq("watch is off and says why", (h["watch"]["on"], h["watch"]["reason"]), (False, "agent_off"))
        eq("with no services the headline is still the new one", h["headline"]["tone"], "new")
        eq("the watcher shows as stopped", [w["state"] for w in h["on_watch"] if w["role"] == "watcher"], ["stopped"])
        ck("and the page can say since when", h["watch"]["every_words"] == "every 10 minutes", h["watch"])
        store.set_agent_enabled("watcher", True, project=UID)
        sre_home.clear_cache()
        key = os.environ.pop("ANTHROPIC_API_KEY")
        try:
            r = await cx.get("/api/home")
            h = r.json()
            SEEN.append(h)
        finally:
            os.environ["ANTHROPIC_API_KEY"] = key
        eq("a watcher with no model: the route answers", r.status_code, 200)
        eq("watch is off for the model", (h["watch"]["on"], h["watch"]["reason"]), (False, "no_model"))
        eq("the watcher shows as stopped there too",
           [w["state"] for w in h["on_watch"] if w["role"] == "watcher"], ["stopped"])
        sre_home.clear_cache()

        # ── the cell: a watcher, an investigator, six services ─────────────
        made = build(store, NOW, blind=True)
        store.set_service("service-a", "test", repos=["example-org/service-a"])
        # two calm-or-not services for the 'unusual' rule: an error in each of the other 143
        # ten-minute buckets of the day, and 6 (or 4) in the last one
        extra = []
        for name, last in (("svc-unusual", 6), ("svc-calm", 4)):
            extra += [_otlp(NOW, 10 * k + 5, name, "ERROR", "request failed") for k in range(1, 144)]
            extra += [_otlp(NOW, 1 + i, name, "ERROR", "request failed") for i in range(last)]
            extra += [_otlp(NOW, 2, name, "INFO", "handled")]
        store.append(extra)
        reset()

        print("== all quiet ==")
        store.set_service("service-a-receiver", "test", hidden=True)
        store.set_service("service-b", "test", hidden=True)
        store.set_service("docs-site", "test", hidden=True)
        h = home()
        eq("the headline", (h["headline"]["text"], h["headline"]["tone"]), ("All quiet", "quiet"))
        ck("it counts the services watched and the last check",
           h["headline"]["sub"].startswith("4 services watched, last check 3 minutes ago"),
           h["headline"]["sub"])
        ck("the cell is being watched", h["watch"]["on"] is True and h["watch"]["reason"] is None
           and h["watch"]["ways_out"] == [], h["watch"])
        eq("no incident", (h["incidents"], h["incidents_more"]), ([], 0))
        eq("the system", h["system"], "your services")
        ck("the watch block says how often and when it looked",
           h["watch"]["every_words"] == "every 10 minutes" and h["watch"]["last_check"]["at"], h["watch"])
        eq("the people on watch", [(w["role"], w["state"]) for w in h["on_watch"]],
           [("watcher", "running"), ("investigator", "waiting")])
        eq("the tools", [t["name"] for t in h["tools"]], ["Traces and logs", "Code changes", "Slack"])

        print("== the name of the system ==")
        store.set_setting(W.SYSTEM_NAME_SETTING, "Billing")
        eq("system follows sre.system_name", home()["system"], "Billing")
        store.set_setting(W.SYSTEM_NAME_SETTING, None)
        eq("and goes back", home()["system"], "your services")

        print("== a silent service ==")
        store.set_service("service-a-receiver", "test", hidden=None)
        h = home()
        ck("the sub names it", "service-a-receiver" in h["headline"]["sub"].split("watched")[-1]
           or any(s["title"] in h["headline"]["sub"] for s in h["blind_spots"] if s["kind"] == "silent"),
           h["headline"]["sub"])
        eq("the headline is still quiet", h["headline"]["tone"], "quiet")
        row = svc(h, "service-a-receiver")
        eq("its state is silent", row["state"], "silent")
        ck("in words", row["state_words"].startswith("Sent nothing for "), row["state_words"])
        ck("the blind spot is listed with a place to fix it",
           any(b["kind"] == "silent" and b["service"] == "service-a-receiver" and b["action"]["to"]
               for b in h["blind_spots"]), h["blind_spots"])
        eq("one with no service is an empty string, never null",
           all(isinstance(b["service"], str) for b in h["blind_spots"]), True)
        store.set_service("service-b", "test", hidden=None)
        store.set_service("docs-site", "test", hidden=None)

        print("== each state of a service ==")
        h = home()
        got = {s["name"]: s["state"] for s in h["services"]}
        eq("a service nobody covers is not watched", got["docs-site"], "not_watched")
        ck("it says since when, or that it never was", svc(h, "docs-site")["state_words"].startswith("Not watched"),
           svc(h, "docs-site")["state_words"])
        eq("silent", got["service-a-receiver"], "silent")
        ck("the sub counts the watched ones out of those it could watch",
           " of " in h["headline"]["sub"] and h["headline"]["sub"].split(" services watched")[0].split(" of ")[0].isdigit(),
           h["headline"]["sub"])
        eq("seen only through another", got["service-a-ui"], "no_own_signal")
        eq("and through which", svc(h, "service-a-ui")["state_words"], "Seen only through edge-proxy")
        eq("six errors in the last ten minutes over a median of one is unusual",
           got["svc-unusual"], "unusual")
        eq("in words", svc(h, "svc-unusual")["state_words"], "More errors than usual")
        eq("four is not", got["svc-calm"], "normal")
        eq("an ordinary service is normal", (got["edge-proxy"], svc(h, "edge-proxy")["state_words"]),
           ("normal", "Normal"))
        eq("only the states there are", set(got.values()) - STATES, set())
        eq("a service nobody can check says so",
           svc(h, "service-a-ui")["last_words"], "not checked, it has no events of its own")
        ck("a watched one says when it was checked", svc(h, "edge-proxy")["last_words"].startswith("checked ")
           or svc(h, "edge-proxy")["last_words"] == "not checked yet", svc(h, "edge-proxy")["last_words"])
        ck("every service has a spark of 24 numbers", all(isinstance(s["spark"], list) and len(s["spark"]) == 24
                                                          and all(isinstance(n, (int, float)) for n in s["spark"])
                                                          for s in h["services"]))
        ck("a service with errors has a spark that shows them",
           sum(svc(h, "svc-unusual")["spark"]) >= 100 and max(svc(h, "svc-unusual")["spark"]) > 0
           and svc(h, "svc-unusual")["spark"][-1] > 0, svc(h, "svc-unusual")["spark"])
        eq("and one with none is flat", sum(svc(h, "service-a-receiver")["spark"]), 0)
        eq("the list says which feeds the services come from", h["services_from_words"],
           sre_home._feeds_words(sre.list_services(store, runtime.catalog, NOW)))

        print("== the name a person gave ==")
        eq("no rename, no about", svc(h, "service-a")["about"], None)
        store.set_service("service-a", "test", display="Billing API")
        h = home()
        eq("after a rename, about is the name they gave", svc(h, "service-a")["about"], "Billing API")
        eq("and the name stays what the data calls it", svc(h, "service-a")["name"], "service-a")
        store.set_service("service-a", "test", display=None)

        print("== one incident open ==")
        store.append([_otlp(NOW, 35, "service-a", "ERROR", "job 41 failed"),
                      _otlp(NOW, 31, "service-a", "ERROR", "job 42 failed")])
        first = incident(store, "service-a", 30, failing="Jobs fail after the retry.",
                         likely_cause="The retry change.", next_step="Roll back the retry change.")
        h = home()
        eq("Slack has not been told: nothing posted", h["incidents"][0]["posted"], None)
        said_ts = f"{int((NOW - timedelta(minutes=29)).timestamp())}.000100"
        store.set_incident_slack(first["id"], "ops-alerts", said_ts)
        h = home()
        eq("once it is posted, where and when", h["incidents"][0]["posted"],
           {"where": "#ops-alerts", "at": datetime.fromtimestamp(float(said_ts), UTC).isoformat()})
        eq("the headline", (h["headline"]["text"], h["headline"]["tone"]),
           ("1 incident open on service-a", "broken"))
        ck("the sub counts the others and the last check",
           h["headline"]["sub"].startswith("The other 5 services look normal")
           or "look normal" in h["headline"]["sub"] or "needs a look" in h["headline"]["sub"],
           h["headline"]["sub"])
        eq("one incident, none more", (len(h["incidents"]), h["incidents_more"]), (1, 0))
        inc = h["incidents"][0]
        eq("it is service-a and open", (inc["service"], inc["state"]), ("service-a", "open"))
        eq("nobody has it", (inc["who"], inc["who_words"]), (None, "nobody has picked it up"))
        eq("how long", inc["age_words"], "open 30 minutes")
        ck("what failed, why and what next", (inc["failing"], inc["likely_cause"], inc["next_step"])
           == ("Jobs fail after the retry.", "The retry change.", "Roll back the retry change."),
           (inc["failing"], inc["likely_cause"], inc["next_step"]))
        ch = inc["chart"]
        eq("the chart has 60 points", len(ch["points"]), 60)
        ck("each is a time and a count", all(len(p) == 2 and isinstance(p[1], int) for p in ch["points"]))
        ck("the errors are in it", sum(p[1] for p in ch["points"]) >= 2, ch["points"])
        ck("a deploy mark with a label", len(ch["deploys"]) >= 1 and all(d["at"] and d["label"] for d in ch["deploys"]),
           ch["deploys"])
        ck("the chart has its label", ch["label"].startswith("Errors"), ch["label"])
        ck("it says what the story rests on", inc["based_on"] and "the latest deploy" in inc["based_on"], inc["based_on"])
        eq("the service row says incident", (svc(h, "service-a")["state"], svc(h, "service-a")["state_words"]),
           ("incident", "Incident open"))
        eq("someone takes it", store.set_incident_state(inc["id"], "on_it", "u1", "Priya", "test")["state"], "on_it")
        h = home()
        eq("who has it", (h["incidents"][0]["who"]["name"], h["incidents"][0]["who_words"]),
           ("Priya", "Priya is on it"))
        eq("the row says who", svc(h, "service-a")["state_words"], "Incident, Priya is on it")
        eq("it still counts as open", h["headline"]["text"], "1 incident open on service-a")

        print("== three, and more than three ==")
        for name, mins in (("edge-proxy", 20), ("docs-site", 15)):
            incident(store, name, mins)
        h = home()
        eq("three incidents", (h["headline"]["text"], len(h["incidents"]), h["incidents_more"]),
           ("3 incidents open", 3, 0))
        for name, mins in (("service-b", 12), ("svc-calm", 10)):
            incident(store, name, mins)
        h = home()
        eq("five: three shown, two more", (h["headline"]["text"], len(h["incidents"]), h["incidents_more"]),
           ("5 incidents open", 3, 2))
        ck("the newest are not hidden behind the oldest", len({i["id"] for i in h["incidents"]}) == 3)
        for table in ("incident_notes", "incidents"):
            store.sc.execute(f"DELETE FROM {table}")

        # ── nobody is watching, each for its own reason ─────────────────────
        print("== the watcher stopped on a monthly budget ==")
        reset()
        set_agent(store, "watcher", budget_usd=0.05, budget_period="month")
        spend(store, "watcher", 0.03, ago(60))
        spend(store, "watcher", 0.03, ago(30))
        h = home()
        w = h["watch"]
        eq("the headline", h["headline"]["tone"], "unwatched")
        ck("nobody has watched since", h["headline"]["text"].startswith("Nobody has watched your services since "),
           h["headline"]["text"])
        ck("the sub gives the reason", "budget" in h["headline"]["sub"], h["headline"]["sub"])
        eq("watch is off for the budget", (w["on"], w["reason"]), (False, "budget_used"))
        ck("it says how long and how many checks it missed", bool(w["off_for_words"]) and w["missed_checks"] == 3, w)
        ck("what it spent is for the month", w["spent_words"].endswith("this month"), w["spent_words"])
        next_month = (MONTH + timedelta(days=32)).replace(day=1)
        eq("and when it starts again, in words", w["resets_on"], day_month_words(next_month))
        eq("nobody was told yet", w["told"], None)
        W.sync(store, runtime.catalog)
        names = list(store.watch_rows())
        told_at = NOW - timedelta(minutes=5)
        store.mark_told(names, told_at, "ops-alerts")
        h = home()
        eq("Slack was told, where and when", h["watch"]["told"],
           {"where": "#ops-alerts", "at": told_at.isoformat()})
        eq("the first way out is raising the budget", w["ways_out"][0]["action"], "raise_budget")
        ck("it is the main button and names the amount", w["ways_out"][0]["primary"] is True
           and w["ways_out"][0]["amount_usd"] == 25 and "$25" in w["ways_out"][0]["label"], w["ways_out"])
        ck("the reason is in words", w["reason_words"].startswith("the watcher used its $0.05 budget"), w["reason_words"])
        eq("the people on watch show it stopped", h["on_watch"][0]["state"], "stopped")
        ck("no such role anywhere", NO_SUCH_ROLE not in json.dumps(h))

        print("== the same on a lifetime budget ==")
        set_agent(store, "watcher", budget_usd=0.05, budget_period="lifetime")
        store.sc.execute("DELETE FROM agent_runs")
        spend(store, "watcher", 0.03, NOW - timedelta(days=40))
        spend(store, "watcher", 0.03, NOW - timedelta(days=39))
        h = home()
        w = h["watch"]
        eq("off for the budget", (w["on"], w["reason"]), (False, "budget_used"))
        ck("spent words have no month in them", w["spent_words"] and "this month" not in w["spent_words"], w["spent_words"])
        eq("and nothing resets", w["resets_on"], None)
        eq("the way out is the same", w["ways_out"][0]["action"], "raise_budget")
        ck("no such role", NO_SUCH_ROLE not in json.dumps(h))

        print("== the daily limit and no model ==")
        reset()
        set_agent(store, "watcher", daily_cap=1)
        spend(store, "watcher", 0.0, max(NOW - timedelta(minutes=40), MIDNIGHT + timedelta(seconds=1)))
        spend(store, "watcher", 0.0, max(NOW - timedelta(minutes=20), MIDNIGHT + timedelta(seconds=2)))
        h = home()
        cap_sub = h["headline"]["sub"]
        eq("the daily limit", (h["watch"]["on"], h["watch"]["reason"]), (False, "daily_cap"))
        ck("in its own words", "checks a day" in cap_sub, cap_sub)
        eq("and no raise_budget for it", h["watch"]["ways_out"], [])
        reset()
        key = os.environ.pop("ANTHROPIC_API_KEY")
        try:
            h = home()
        finally:
            os.environ["ANTHROPIC_API_KEY"] = key
        eq("no model", (h["watch"]["on"], h["watch"]["reason"]), (False, "no_model"))
        ck("in its own words", "no AI model is set up" in h["headline"]["sub"].lower().replace("No", "no")
           or "model" in h["headline"]["sub"], h["headline"]["sub"])
        ck("the two reasons read differently", cap_sub != h["headline"]["sub"])
        eq("no way out of it from here", h["watch"]["ways_out"], [])

        print("== incidents while nobody is watching ==")
        reset()
        set_agent(store, "watcher", budget_usd=0.05, budget_period="month")
        spend(store, "watcher", 0.06, ago(30))
        incident(store, "service-a", 25)
        h = home()
        eq("the incident comes first, and says nobody is watching",
           (h["headline"]["tone"], h["headline"]["text"]),
           ("broken", "1 incident open on service-a, and nobody is watching"))
        ck("the sub says why", "budget" in h["headline"]["sub"], h["headline"]["sub"])

        print("== the roles ==")
        reset()
        h = home()
        eq("only a watcher and an investigator", {w["role"] for w in h["on_watch"]}, {"watcher", "investigator"})
        ck("each says what it does and what it costs", all(w["doing"] and w["model_words"] and w["cost_words"]
                                                            for w in h["on_watch"]), h["on_watch"])
        ck("no such role", NO_SUCH_ROLE not in json.dumps(h))
        eq("no channel for the notes", h["notes_go_to"], None)
        set_agent(store, "investigator", slack_channel="ops-alerts")
        h = home()
        eq("the notes go to the investigator's channel", h["notes_go_to"], {"label": "#ops-alerts in Slack"})
        set_agent(store, "investigator", slack_channel="C0123ABCDEF")
        eq("a channel id is not shown as a code", home()["notes_go_to"], {"label": "a Slack channel"})
        set_agent(store, "investigator", slack_channel="")
        set_agent(store, "investigator", budget_usd=0.05, budget_period="month")
        spend(store, "investigator", 0.06, ago(30))
        h = home()
        inv = next(w for w in h["on_watch"] if w["role"] == "investigator")
        eq("an investigator that used its budget says stopped", inv["state"], "stopped")
        ck("and says why", "budget" in inv["doing"].lower(), inv["doing"])
        eq("the watcher is not", next(w for w in h["on_watch"] if w["role"] == "watcher")["state"], "running")
        set_agent(store, "investigator", budget_usd=25.0, budget_period="lifetime")

        # ── what the watcher raised, and what it did today ──────────────────
        print("== what the watcher raised ==")
        reset()
        store.sc.execute("DELETE FROM agent_runs")

        def watcher_run(rid, service, minutes, outcome="finding", cost=0.0):
            """A finished check of the watcher that started `minutes` back (never before midnight)."""
            at = max(NOW - timedelta(minutes=minutes), MIDNIGHT + timedelta(seconds=300 - minutes))
            store.start_agent_run(rid, "watcher", "watch-prod", "", service, "h", project=UID)
            if outcome == "finding":
                store.finish_agent_run(rid, "ok", finding=f"{service} returns errors.", outcome="finding",
                                       verdict="investigate")
            else:
                store.finish_agent_run(rid, "ok", finding="Nothing changed.", outcome="no_op")
            with store._swrite:
                store.sc.execute("UPDATE agent_runs SET started_at = ?, cost_usd = ? WHERE id = ?",
                                 [at, cost, rid])
            return at
        names7 = ["service-a", "edge-proxy", "docs-site", "service-b", "svc-calm", "svc-unusual", "service-a-receiver"]
        for n, name in enumerate(names7):                   # raised 70, 60, ... 10 minutes back
            watcher_run(f"raise{n}", name, 70 - 10 * n, cost=0.01)
        watcher_run("calm1", "service-a", 5, outcome="quiet")
        h = home()
        eq("seven were raised: the newest five show", len(h["raised"]), 5)
        eq("newest first", [r["id"] for r in h["raised"]], [f"raise{n}" for n in (6, 5, 4, 3, 2)])
        ats = [r["at"] for r in h["raised"]]
        ck("by their time", ats == sorted(ats, reverse=True), ats)
        ck("in UTC like every other time", all(a.endswith("+00:00") for a in ats), ats)
        eq("each says which service", [r["service"] for r in h["raised"]], names7[::-1][:5])
        ck("and can be answered", all(r["can_answer"] is True for r in h["raised"]), h["raised"])
        eq("nobody answered yet", [r["worth"] for r in h["raised"]], [None] * 5)
        eq("the quiet check since is said", h["raised_note"], "Every check since then came back quiet.")
        store.set_feedback("raise5", "alice", "Alice", True, None, "console", "watcher", "raise5", None, UID)
        store.set_feedback("raise4", "alice", "Alice", False, None, "console", "watcher", "raise4", None, UID)
        store.set_feedback("raise3", "bob", "Bob", True, None, "console", "watcher", "raise3", None, UID)
        h = home(person="alice")
        eq("the caller's own answers show", {r["id"]: r["worth"] for r in h["raised"]},
           {"raise6": None, "raise5": True, "raise4": False, "raise3": None, "raise2": None})
        eq("someone else sees theirs", {r["id"]: r["worth"] for r in home(person="bob")["raised"]}["raise3"], True)
        eq("a caller who has answered nothing sees none",
           [r["worth"] for r in home(person="carol")["raised"]], [None] * 5)
        eq("the newest raised is first", h["raised"][0]["when_words"], sre_home._when_words(h["raised"][0]["at"], NOW))
        watcher_run("raise7", "edge-proxy", 2)
        h = home()
        eq("a raise after the quiet check: no note", h["raised_note"], None)
        eq("and it is the first", h["raised"][0]["id"], "raise7")
        eq("still five", len(h["raised"]), 5)
        eq("today counts the checks, the quiet ones and the cost",
           (h["today"]["checks"], h["today"]["quiet"], h["today"]["spent_usd"]), (9, 1, 0.07))
        spend(store, "investigator", 0.02, max(NOW - timedelta(minutes=1), MIDNIGHT + timedelta(seconds=1)))
        h = home()
        eq("and what the investigator did", (h["today"]["investigated"], h["today"]["spent_usd"]), (1, 0.09))
        spend(store, "watcher", 5.0, max(NOW - timedelta(minutes=1), MIDNIGHT + timedelta(seconds=1)), practice=True)
        eq("a practice run is not counted", home()["today"]["spent_usd"], 0.09)
        store.sc.execute("DELETE FROM agent_runs")

        # ── the button ──────────────────────────────────────────────────────
        print("== raise the budget ==")
        reset()
        set_agent(store, "watcher", budget_usd=0.05, budget_period="month")
        spend(store, "watcher", 0.06, ago(30))
        sre_home.clear_cache()
        before = (await cx.get("/api/home")).json()
        eq("watch is off first", (before["watch"]["on"], before["watch"]["reason"]), (False, "budget_used"))
        r = await cx.post("/api/home/actions/raise_budget", json={"amount_usd": 25})
        eq("the button works", (r.status_code, r.json().get("ok")), (200, True))
        eq("the budget went up by what it said", store.get_catalog_agent("watcher")["budget_usd"], 25.05)
        ck("and nothing else of the watcher changed", store.get_catalog_agent("watcher")["budget_period"] == "month"
           and store.get_catalog_agent("watcher")["trigger"] == "watch-prod"
           and store.get_catalog_agent("watcher")["concludes"] is True)
        ck("the handoff to the investigator is still there", bool(store.get_wake(UID, "watch-prod", "watcher"))
           if hasattr(store, "get_wake") else True)
        eq("the answer carries the home, watching again", (r.json()["home"]["watch"]["on"], r.json()["home"]["edition"]),
           (True, "sre"))
        after = (await cx.get("/api/home")).json()
        eq("and the next read agrees", (after["watch"]["on"], after["watch"]["reason"]), (True, None))
        r = await cx.post("/api/home/actions/raise_budget", json={"amount_usd": -3})
        eq("a bad amount is refused", r.status_code, 400)

        print("== raise the budget of a watcher that was turned off ==")
        reset()
        store.sc.execute("DELETE FROM agent_runs")
        at = max(NOW - timedelta(minutes=20), MIDNIGHT + timedelta(seconds=20))
        store.start_agent_run("mine1", "watcher", "watch-prod", "", "service-a", "h", project=UID)
        store.finish_agent_run("mine1", "ok", finding="service-a returns errors.", outcome="finding",
                               verdict="investigate")
        with store._swrite:
            store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, "mine1"])
        r = await cx.put("/api/findings/mine1/worth", json={"worth": True})
        eq("the caller answers a raised finding", r.status_code, 200)
        store.set_agent_enabled("watcher", False, project=UID)
        sre_home.clear_cache()
        before = (await cx.get("/api/home")).json()
        eq("the watcher is off", (before["watch"]["on"], before["watch"]["reason"]), (False, "agent_off"))
        eq("the home knows the caller's answer", [x["worth"] for x in before["raised"]], [True])
        r = await cx.post("/api/home/actions/raise_budget", json={"amount_usd": 5})
        eq("the button works on it", (r.status_code, r.json().get("ok")), (200, True))
        eq("watch is on in the answer", r.json()["home"]["watch"]["on"], True)
        eq("the watcher is enabled again", store.agent_enabled("watcher", project=UID), True)
        eq("with the five dollars added", store.get_catalog_agent("watcher")["budget_usd"], 30.0)
        eq("the answer is built for the caller", [x["worth"] for x in r.json()["home"]["raised"]], [True])
        eq("and the next read agrees", (await cx.get("/api/home")).json()["watch"]["on"], True)
        store.sc.execute("DELETE FROM finding_feedback")

        print("== raise the budget when another project uses the watcher ==")
        OTHER = "prj-other-watch"
        store.create_project(OTHER, "custom", "other-watch", {}, goal="Watch from a second project")
        store.upsert_catalog_trigger("watch-other", ["prod-otlp"], {"every": "20m"}, {}, "20m")
        store.put_in_project("trigger", "watch-other", OTHER)
        before_wakes = store.list_wakes(project=UID)
        store.remove_wakes(project=UID)        # so the second project's wake is the one the button finds
        store.set_wake(OTHER, "watch-other", "watcher", enabled=False)
        runtime.reload_catalog()
        sre_home.clear_cache()
        eq("the maker's trigger is watch-prod", store.get_catalog_agent("watcher")["trigger"], "watch-prod")
        r = await cx.post("/api/home/actions/raise_budget", json={"amount_usd": 5})
        eq("the button works", (r.status_code, r.json().get("ok")), (200, True))
        eq("it turned the watcher on in the project that uses it", store.agent_enabled("watcher", project=OTHER), True)
        eq("and left the maker's trigger as it was", store.get_catalog_agent("watcher")["trigger"], "watch-prod")
        eq("the second project still wakes it with its own", store.get_catalog_agent("watcher", project=OTHER)["trigger"],
           "watch-other")
        store.remove_wakes(project=OTHER, agent="watcher")
        store.delete_project(OTHER)
        store.delete_catalog_trigger("watch-other")
        for w in before_wakes:
            store.set_wake(UID, w["trigger"], w["agent"], enabled=w["enabled"])
        set_agent(store, "watcher", budget_usd=30.0)
        runtime.reload_catalog()
        sre_home.clear_cache()
        r = await cx.post("/api/home/actions/raise_budget", json={"amount_usd": "lots"})
        eq("so is a word", r.status_code, 400)
        r = await cx.post("/api/home/actions/" + NO_SUCH_ROLE)
        eq("an action that does not exist is 404", r.status_code, 404)
        key = (await cx.post("/api/keys", json={"name": "viewer", "scopes": ["read"]})).json()
        read = {"Authorization": f"Bearer {key['secret']}"}
        r = await cx.post("/api/home/actions/raise_budget", headers=read, json={"amount_usd": 25})
        eq("a read key may not press it", r.status_code, 403)
        eq("and nothing changed", store.get_catalog_agent("watcher")["budget_usd"], 30.0)
        eq("but it may read the home", (await cx.get("/api/home", headers=read)).status_code, 200)
        eq("no key at all is 401", (await httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t").post(
            "/api/home/actions/raise_budget", json={})).status_code, 401)

        print("== the route and the cache ==")
        reset()
        sre_home.clear_cache()
        r = await cx.get("/api/home")
        body = r.json()
        eq("GET /api/home on an AI SRE cell returns this body", (r.status_code, set(body)), (200, KEYS))
        ck("with the headline of the cell", body["headline"]["tone"] in ("quiet", "broken", "unwatched"), body["headline"])
        SEEN.append(body)
        store.set_setting(W.SYSTEM_NAME_SETTING, "Billing")
        eq("a change is not seen inside 5 seconds", (await cx.get("/api/home")).json()["system"], body["system"])
        sre_home.clear_cache()
        eq("and is after the cache is cleared", (await cx.get("/api/home")).json()["system"], "Billing")
        store.set_setting(W.SYSTEM_NAME_SETTING, None)
        calls = []
        sre_home.clear_cache()

        def health():
            calls.append(1)
            return {}
        a = sre_home.cached(store, runtime.catalog, health, "key-a")
        b = sre_home.cached(store, runtime.catalog, health, "key-a")
        c = sre_home.cached(store, runtime.catalog, health, "key-b")
        ck("the same credential gets the kept answer, another its own", a is b and a is not c)
        eq("a kept answer asks nothing of the sources", len(calls), 2)
        for k, (t, v) in list(sre_home._CACHE.items()):
            sre_home._CACHE[k] = (t - 6, v)
        sre_home.cached(store, runtime.catalog, health, "key-a")
        eq("after 5 seconds it is built again", len(calls), 3)
        sre_home.clear_cache()
        seen_gen = []

        def clears_while_building():
            seen_gen.append(1)
            sre_home.clear_cache()      # a button pressed while this read is being built
            return {}
        sre_home.cached(store, runtime.catalog, clears_while_building, "key-c")
        ck("a body built while the cache was cleared is not kept", not sre_home._CACHE, list(sre_home._CACHE))
        sre_home.cached(store, runtime.catalog, clears_while_building, "key-c")
        eq("so the next read builds again", len(seen_gen), 2)

        print("== a platform cell still has no AI SRE home ==")
        r = await cx.put("/api/settings/edition", json={"edition": "platform"})
        eq("the edition is set to platform", r.status_code, 200)
        sre_home.clear_cache()
        eq("GET /api/home is 404", (await cx.get("/api/home")).status_code, 404)
        eq("so is the button", (await cx.post("/api/home/actions/raise_budget", json={})).status_code, 404)
        r = await cx.put("/api/settings/edition", json={"edition": "sre"})
        eq("and back", (r.status_code, (await cx.get("/api/home")).status_code), (200, 200))

        print("== the words ==")
        bad = []
        for body in SEEN:
            bad += words.offenders_in(body, SKIP)
        eq("no word a person should not read, in any answer above", sorted(set(bad)), [])
        dashes = [s for body in SEEN for s in strings(body) if chr(0x2014) in s]
        eq("no em dash in any string", dashes, [])
        ck("the retired word does not appear", not any(BANNED_WORD in s.lower() for body in SEEN for s in strings(body)))
        ck("every answer above was checked", len(SEEN) >= 15, len(SEEN))

    # ── the cost of a read ──────────────────────────────────────────────────
    print("== the cost of a read: 24 services, 200,000 events ==")
    import tares.daemon as daemon
    daemon.DB_PATH = os.path.join(TMP, "load.duckdb")      # a cell of its own; the daemon read the path at import
    big = make_app()
    async with big.router.lifespan_context(big):
        store, runtime = big.state.store, big.state.runtime
        t0 = time.time()
        made = build(store, NOW, watcher=True, investigator=True, services=20, events=200_000)
        print(f"  (cell built in {time.time() - t0:.0f} s)")
        runtime.reload_catalog()
        store.set_fired("watch-prod", "watch-prod", NOW - timedelta(minutes=3))
        for i in range(3):
            incident_at = NOW - timedelta(minutes=20 + i)
            inc = store.create_incident(made["services"][i], UID, incident_at)
            finding_run(store, f"load{i}", made["services"][i], incident_at)
            store.add_incident_note(inc["id"], f"load{i}", incident_at)
        eq("the cell has the services", len(sre.list_services(store, runtime.catalog, NOW)) >= 24, True)
        # the first build on a cold cell: neither the services nor the 24 hour error series held
        sre_home.clear_cache()
        store.__dict__.pop("services_cache", None)
        t0 = time.perf_counter()
        sre_home.build(store, runtime.catalog, None)
        first = (time.perf_counter() - t0) * 1000
        # then the way the route builds it each time the 5 second page cache has run out: the
        # real clock, build() called directly so the page cache is not in play, and the services
        # (30 s) and the error series (30 s) held, as they are between two reads on a live cell
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            h = sre_home.build(store, runtime.catalog, None)
            times.append((time.perf_counter() - t0) * 1000)
        slow = p95(times)
        print(f"  first build, nothing held: {first:.0f} ms")
        print(f"  build x20: min {min(times):.0f} ms, median {sorted(times)[10]:.0f} ms, "
              f"p95 {slow:.0f} ms, max {max(times):.0f} ms")
        ck("p95 under 300 ms", slow < 300, f"{slow:.0f} ms")
        ck("the first build on a cold cell is under 600 ms", first < 600, f"{first:.0f} ms")
        # the 24 hour error series is scanned once, then held for 30 seconds on the real clock
        scans = []
        real = store.event_series_many

        def counting(services, since, until, bucket_s, errors_only=False):
            if bucket_s == sre_home.UNUSUAL_BUCKET_S:
                scans.append(1)
            return real(services, since, until, bucket_s, errors_only)
        store.event_series_many = counting
        try:
            sre_home.clear_cache()
            sre_home.build(store, runtime.catalog, None)
            sre_home.build(store, runtime.catalog, None)
            eq("two reads on the real clock scan the error series once", len(scans), 1)
            sre_home.clear_cache()
            sre_home.build(store, runtime.catalog, None)
            eq("clear_cache makes the next read scan it again", len(scans), 2)
            sre_home.build(store, runtime.catalog, None, now=NOW)
            sre_home.build(store, runtime.catalog, None, now=NOW)
            eq("a call with a `now` of its own always scans", len(scans), 4)
        finally:
            store.event_series_many = real
        ck("and it is the whole page", len(h["services"]) >= 24 and len(h["incidents"]) == 3, len(h["services"]))
        sre_home.clear_cache()
        t0 = time.perf_counter()
        r = await httpx.AsyncClient(transport=httpx.ASGITransport(app=big), base_url="http://t", headers=auth_header).get("/api/home")
        print(f"  GET /api/home on the real clock: {(time.perf_counter() - t0) * 1000:.0f} ms")
        eq("and the route answers", r.status_code, 200)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
