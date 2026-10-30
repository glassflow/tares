"""Knowing, and saying, when nobody is watching (TR-743).

The roles a cell's agents play (watchers and investigators) and which services a watcher covers
(a trigger filter, a paused project), the answer for each service (watched, or the reason it is
not: budget used, daily limit, turned off, paused, no model, a schedule that stopped, no watcher),
the stored state and what changed since the last look, the one Slack message about a change (to
the investigator's channel, once, "Watching again" when it ends), a post that fails and is tried
again, two callers at once, the name of the system, a cell that is not an AI SRE cell (keeps the
state, says nothing), the words, GET /api/watch, the watch on GET /api/services and the daemon's
loop.

The model loop is a stand-in and Slack is a stub server that records what it is sent. Time moves
through the `now` the functions take; spend is a run row with a chosen started_at.

Run: .venv/bin/python tests/test_watch_state.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ

scrub_os_environ()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SLACK = []                 # every body the stub was sent
MODE = {"answer": "ok"}    # "ok", or "fail" for ok:false


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SLACK.append(body)
        out = ({"ok": False, "error": "channel_not_found"} if MODE["answer"] == "fail"
               else {"ok": True, "channel": body.get("channel"), "ts": f"1790000000.{len(SLACK):06d}"})
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


_server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=_server.serve_forever, daemon=True).start()

TMP = tempfile.mkdtemp()
os.environ["TARES_SLACK_API_BASE"] = f"http://127.0.0.1:{_server.server_address[1]}"
os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-test"
os.environ["TARES_PUBLIC_URL"] = "https://cell.example.com"
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_SCHEDULE_CLOCK_SECONDS"] = "3600"   # the clock must not fire the watcher under us
os.environ["TARES_WATCH_SECONDS"] = "3600"             # nor the watch loop; a test steps it by hand
# a provider must resolve for a watcher to run; the stand-in loop never calls it
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx

import tares.builtin_agents as ba
from fixtures.sre_cell import PROJECT, build
from tares import edition as edition_mod
from tares import sre, watch as W
from tares import words
from tares.builtin_agents import AgentRunner
from tares.store import Store

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
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
FIRST = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
UID = "prj-" + PROJECT
SERVICES = ["edge-proxy", "service-a"]            # service-a-receiver is hidden below
HANDOFFS = [{"verdict": "investigate", "agent": "investigator", "cooldown": "1h"}]
BUDGET_WORDS = "the watcher used its $0.05 budget for October; it starts again on 1 Nov"
ALL_WORDS: list = []                               # every string a person would read, checked last


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


def fire(store, when):
    store.set_fired("watch-prod", "watch-prod", when)


def posts():
    """The stub's bodies, as text: header, lines and the context line."""
    return [json.dumps(b) for b in SLACK]


def lines(body):
    return body["blocks"][1]["text"]["text"]


def step(store, cat, now):
    """One look and one telling, what the loop does: (what changed, what was posted)."""
    changes = W.sync(store, cat, now)
    return changes, W.post(store, cat, changes, now)


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        store = app.state.store
        runtime = app.state.runtime
        real_clock = W.clock

        print("== an empty cell ==")
        r = await cx.get("/api/watch")
        eq("GET /api/watch on an empty cell", (r.status_code, r.json()["watching"], r.json()["services"],
                                              r.json()["watchers"]), (200, "nothing", [], []))
        eq("the system is called your services", r.json()["system_name"], "your services")

        build(store, NOW, watcher=True, investigator=True)
        runtime.reload_catalog()
        cat = runtime.catalog
        # the doc's two services: the fixture's third (service-a-receiver, silent for two days) is
        # on the same source, so it is covered like the others; hide it so the budget stop names two
        store.set_service("service-a-receiver", "test", hidden=True)
        set_agent(store, "watcher", budget_usd=0.05, budget_period="month", slack_channel="C111")
        set_agent(store, "investigator", slack_channel="C222")
        eq("edition set to sre", (await cx.put("/api/settings/edition", json={"edition": "sre"})).status_code, 200)

        async def base(lifetime=False):
            """The cell as every case starts: the watcher on and running, nothing spent, no state."""
            r = await cx.post(f"/api/projects/{UID}/resume")
            assert r.status_code == 200, r.text
            cat_now = runtime.catalog
            store.sc.execute("DELETE FROM watch_state")
            store.sc.execute("DELETE FROM agent_runs")
            store.set_wake(UID, "watch-prod", "watcher", enabled=True)
            store.set_setting(edition_mod.EDITION_SETTING, "sre")
            store.set_setting(W.SYSTEM_NAME_SETTING, None)
            store.set_setting(W.CHANNEL_SETTING, None)
            set_agent(store, "watcher", budget_usd=0.05, daily_cap=None, slack_channel="C111",
                      budget_period="lifetime" if lifetime else "month")
            fire(store, NOW - timedelta(minutes=3))
            W._ATTEMPTS.clear()
            W.clock = real_clock
            MODE["answer"] = "ok"
            SLACK.clear()
            return cat_now

        def two_runs(when_second=None):
            second = when_second or NOW - timedelta(minutes=10)
            spend(store, "watcher", 0.03, second - timedelta(minutes=10))
            spend(store, "watcher", 0.03, second)
            return second

        def rows():
            return store.watch_rows()

        print("== who watches, from how things are set up ==")
        got = sre.roles(store, cat)
        eq("the watcher is found", [(w["agent"], w["project"], w["trigger"]) for w in got["watchers"]],
           [("watcher", UID, "watch-prod")])
        eq("the investigator is found", got["investigators"], [{"agent": "investigator", "project": UID}])
        items = {i["name"]: i for i in sre.list_services(store, cat, NOW)}
        eq("a service on the watcher's source is watched, and by whom",
           (items["service-a"]["watched"], items["service-a"]["watched_by"]),
           (True, [{"agent": "watcher", "project": UID}]))
        eq("a service on another source has no watcher",
           (items["docs-site"]["watched"], items["docs-site"]["watched_by"]), (False, []))
        store.upsert_catalog_trigger("watch-prod", ["prod-otlp"], {"every": "10m"}, {}, "10m",
                                     filters=[{"field": "service", "op": "eq", "value": "service-a"}])
        runtime.reload_catalog()
        cat = runtime.catalog
        items = {i["name"]: i for i in sre.list_services(store, cat, NOW)}
        eq("a filter service eq service-a covers service-a only",
           {n: items[n]["watched"] for n in ("service-a", "edge-proxy")},
           {"service-a": True, "edge-proxy": False})
        store.upsert_catalog_trigger("watch-prod", ["prod-otlp"], {"every": "10m"}, {}, "10m")
        runtime.reload_catalog()
        cat = runtime.catalog
        r = await cx.post(f"/api/projects/{UID}/pause")
        eq("pausing the project", r.status_code, 200)
        cat = runtime.catalog
        eq("a paused project covers nothing", sre.get_service(store, cat, "service-a", NOW)["watched"], False)
        eq("the paused watcher is still listed when asked", len(sre.roles(store, cat, include_paused=True)["watchers"]), 1)
        eq("and not otherwise", sre.roles(store, cat)["watchers"], [])

        print("== a healthy cell tells nobody ==")
        cat = await base()
        changes, said = step(store, cat, NOW)
        eq("only the service nobody covers is a change", [c["service"] for c in changes], ["docs-site"])
        eq("and it has no channel to go to", (said, SLACK), ([], []))
        eq("the watched services are kept as told",
           {n: (rows()[n]["watched"], rows()[n]["told_at"] is not None) for n in SERVICES},
           {n: (True, True) for n in SERVICES})
        eq("the one nobody covers waits for a channel",
           (rows()["docs-site"]["watched"], rows()["docs-site"]["reason"], rows()["docs-site"]["told_at"]),
           (False, "no_agent", None))
        ck("a hidden service has no row", "service-a-receiver" not in rows())

        print("== the budget runs out ==")
        second = two_runs()
        changes, said = step(store, cat, NOW)
        eq("two services stop", sorted(c["service"] for c in changes), SERVICES)
        eq("both for the budget", {c["reason"] for c in changes}, {"budget_used"})
        eq("since the run that used it up", {W._aware(c["since"]) for c in changes}, {second})
        eq("one post, to the investigator's channel", [(s["channel"]) for s in SLACK], ["C222"])
        eq("naming both", said, [{"channel": "C222", "stopped": SERVICES, "started": []}])
        body = SLACK[0]
        eq("the headline", body["blocks"][0]["text"]["text"], "Nobody is watching 2 services")
        ck("the line names both services", "edge-proxy, service-a: not watched since 15 Oct, 11:50" in lines(body), lines(body))
        ck("and says why, with the month", BUDGET_WORDS in lines(body), lines(body))
        ck("the receiver nobody sees is not named", "service-a-receiver" not in posts()[0])
        ck("it names the system", "AI SRE for your services" in posts()[0])
        eq("the rows say where they were told", {rows()[n]["told_where"] for n in SERVICES}, {"C222"})
        ALL_WORDS.extend([BUDGET_WORDS, lines(body)])

        changes, said = step(store, cat, NOW + timedelta(minutes=1))
        eq("a second look changes nothing", (changes, said, len(SLACK)), ([], [], 1))

        print("== when a limit was reached ==")
        t1, t2 = NOW - timedelta(hours=6), NOW - timedelta(hours=5)
        spend(store, "unit-b", 0.025, t1)
        spend(store, "unit-b", 0.025, t2)
        eq("a budget met exactly is reached at the run that met it",
           W._budget_reached_at(store, {"name": "unit-b", "budget_usd": 0.05}, NOW), W._aware(t2))
        spend(store, "unit-c", 0.0, NOW - timedelta(hours=9))
        spend(store, "unit-c", 0.0, NOW - timedelta(hours=3), status="capped")
        spend(store, "unit-c", 0.0, NOW - timedelta(hours=1), status="capped")
        eq("the limit is reached at the first run it refused, not an ordinary run",
           W._cap_reached_at(store, "unit-c", NOW), W._aware(NOW - timedelta(hours=3)))

        print("== the first of the month ==")
        fire(store, FIRST - timedelta(minutes=3))
        changes, said = step(store, cat, FIRST)
        eq("the month budget is free again: both are watched", sorted(c["service"] for c in changes), SERVICES)
        eq("one more post", len(SLACK), 2)
        eq("it says watching again", SLACK[1]["blocks"][0]["text"]["text"], "Watching again")
        ck("naming both", "Watching again: edge-proxy, service-a." in lines(SLACK[1]), lines(SLACK[1]))
        eq("it went to the same channel", SLACK[1]["channel"], "C222")
        changes, said = step(store, cat, FIRST + timedelta(minutes=1))
        eq("and then nothing", (changes, len(SLACK)), ([], 2))

        print("== a lifetime budget does not start again ==")
        cat = await base(lifetime=True)
        step(store, cat, NOW)
        SLACK.clear()
        two_runs()
        changes, said = step(store, cat, NOW)
        eq("it stops", sorted(c["service"] for c in changes), SERVICES)
        ck("the words carry no month", lines(SLACK[0]).endswith("the watcher used its $0.05 budget."), lines(SLACK[0]))
        fire(store, FIRST - timedelta(minutes=3))
        changes, said = step(store, cat, FIRST)
        eq("on the 1st it is still stopped", (changes, len(SLACK)), ([], 1))
        eq("with the same reason", {rows()[n]["reason"] for n in SERVICES}, {"budget_used"})
        ALL_WORDS.append(lines(SLACK[0]))

        print("== raising the budget ==")
        cat = await base()
        step(store, cat, NOW)
        two_runs()
        step(store, cat, NOW)
        eq("stopped and told", len(SLACK), 1)
        set_agent(store, "watcher", budget_usd=1.0)
        changes, said = step(store, cat, NOW + timedelta(minutes=1))
        eq("both are watched again", sorted(c["service"] for c in changes), SERVICES)
        eq("one more post that says so", [b["blocks"][0]["text"]["text"] for b in SLACK],
           ["Nobody is watching 2 services", "Watching again"])

        print("== the other reasons ==")
        cat = await base()
        step(store, cat, NOW)
        store.set_wake(UID, "watch-prod", "watcher", enabled=False)
        step(store, cat, NOW)
        eq("the wake turned off", {rows()[n]["reason"] for n in SERVICES}, {"agent_off"})
        ck("in words", "the watcher is turned off." in lines(SLACK[-1]), lines(SLACK[-1]))
        ALL_WORDS.append(lines(SLACK[-1]))

        cat = await base()
        step(store, cat, NOW)
        eq("the project is paused", (await cx.post(f"/api/projects/{UID}/pause")).status_code, 200)
        cat = runtime.catalog
        step(store, cat, None)
        eq("paused", {rows()[n]["reason"] for n in SERVICES}, {"project_paused"})
        logged = next(e["at"] for e in store.list_project_log(UID, limit=20) if e["action"] == "paused")
        eq("since the pause was logged", {W._aware(rows()[n]["since"]) for n in SERVICES}, {W._aware(logged)})
        ck("in words", "watching is paused." in lines(SLACK[-1]), lines(SLACK[-1]))
        ALL_WORDS.append(lines(SLACK[-1]))

        cat = await base()
        step(store, cat, NOW)
        real = datetime.now(UTC)
        for i in range(49):
            spend(store, "watcher", 0.0, real - timedelta(minutes=5 + i))
        for i in range(3):
            spend(store, "watcher", 0.0, real - timedelta(minutes=60 + i), practice=True)
        set_agent(store, "watcher", daily_cap=50)
        step(store, cat, None)
        ck("49 runs and 3 practice runs under a cap of 50: practice does not count",
           "daily_cap" not in {rows()[n]["reason"] for n in SERVICES}, str({rows()[n]["reason"] for n in SERVICES}))
        spend(store, "watcher", 0.0, real - timedelta(minutes=70))
        step(store, cat, None)
        eq("50 runs today with a cap of 50", {rows()[n]["reason"] for n in SERVICES}, {"daily_cap"})
        ck("in words, with when it frees",
           "the watcher reached its limit of 50 checks a day; it starts again in " in lines(SLACK[-1]), lines(SLACK[-1]))
        ALL_WORDS.append(lines(SLACK[-1]))

        cat = await base()
        step(store, cat, NOW)
        key = os.environ.pop("ANTHROPIC_API_KEY")
        try:
            step(store, cat, NOW)
        finally:
            os.environ["ANTHROPIC_API_KEY"] = key
        eq("no model", {rows()[n]["reason"] for n in SERVICES}, {"no_model"})
        ck("in words", "no AI model is set up." in lines(SLACK[-1]), lines(SLACK[-1]))
        ALL_WORDS.append(lines(SLACK[-1]))

        cat = await base()
        step(store, cat, NOW)
        fire(store, NOW - timedelta(minutes=50))
        step(store, cat, NOW)
        eq("a schedule five ticks behind", {rows()[n]["reason"] for n in SERVICES}, {"schedule_not_firing"})
        eq("since the tick that did not happen", {W._aware(rows()[n]["since"]) for n in SERVICES},
           {NOW - timedelta(minutes=40)})
        ck("in words", "the scheduled check has not happened since 11:10." in lines(SLACK[-1]), lines(SLACK[-1]))
        ALL_WORDS.append(lines(SLACK[-1]))
        fire(store, NOW - timedelta(minutes=3))
        step(store, cat, NOW)
        eq("it ticks again: watched", {rows()[n]["watched"] for n in SERVICES}, {True})

        print("== a stop that was already there ==")
        cat = await base()
        two_runs()
        changes, said = step(store, cat, NOW)
        eq("a first look at a watcher out of budget posts once", [(s["channel"], lines(s)[:11]) for s in SLACK],
           [("C222", "edge-proxy,")])
        step(store, cat, NOW)
        eq("and only once", len(SLACK), 1)

        print("== Slack says no ==")
        cat = await base()
        two_runs()
        MODE["answer"] = "fail"
        t0 = 1000.0
        W.clock = lambda: t0
        changes, said = step(store, cat, NOW)
        eq("it tried and was refused", (len(SLACK), said), (1, []))
        eq("the rows are still untold", {rows()[n]["told_at"] for n in SERVICES}, {None})
        W.clock = lambda: t0 + 300
        step(store, cat, NOW)
        eq("inside ten minutes it does not try again", len(SLACK), 1)
        MODE["answer"] = "ok"
        W.clock = lambda: t0 + 601
        changes, said = step(store, cat, NOW)
        eq("after ten minutes it tries and Slack takes it", (len(SLACK), said),
           (2, [{"channel": "C222", "stopped": SERVICES, "started": []}]))
        eq("the rows are told", {rows()[n]["told_at"] is not None for n in SERVICES}, {True})
        W.clock = real_clock

        print("== two callers at once ==")
        cat = await base()
        two_runs()
        gate = threading.Barrier(2)

        def caller():
            gate.wait()
            step(store, cat, NOW)

        ts = [threading.Thread(target=caller) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        eq("one post", len(SLACK), 1)

        print("== the name of the system ==")
        os.environ.pop("TARES_INSTANCE_NAME", None)
        store.set_setting(W.SYSTEM_NAME_SETTING, None)
        eq("nothing set", W.system_name(store), "your services")
        os.environ["TARES_INSTANCE_NAME"] = "cell-7"
        eq("the instance name", W.system_name(store), "cell-7")
        store.set_setting(W.SYSTEM_NAME_SETTING, "  Project X  ")
        eq("the setting wins, trimmed", W.system_name(store), "Project X")
        cat = await base()
        store.set_setting(W.SYSTEM_NAME_SETTING, "Project X")
        two_runs()
        step(store, cat, NOW)
        ck("the message names it", "AI SRE for Project X" in posts()[0], posts()[0])
        os.environ.pop("TARES_INSTANCE_NAME", None)

        print("== a service nobody covers, and no watcher at all ==")
        cat = await base()
        step(store, cat, NOW)
        store.set_setting(W.CHANNEL_SETTING, "#C333")
        step(store, cat, NOW)
        eq("it goes to the channel the cell names", [s["channel"] for s in SLACK], ["C333"])
        ck("naming it", "docs-site: not watched since 15 Oct, 12:00, no watcher is set up for it." in lines(SLACK[0]), lines(SLACK[0]))
        ALL_WORDS.append(lines(SLACK[0]))
        cat = await base()
        store.delete_catalog_agent("watcher")
        # the fixture's investigator is also wired to the schedule, switched off; with the watcher
        # gone it would read as a watcher that was turned off, so it goes too
        store.delete_catalog_agent("investigator")
        runtime.reload_catalog()
        cat = runtime.catalog
        changes, said = step(store, cat, NOW)
        eq("without the watcher nothing is watched", sorted(c["service"] for c in changes),
           ["docs-site", "edge-proxy", "service-a"])
        eq("for lack of a watcher", {c["reason"] for c in changes}, {"no_agent"})
        eq("and there is nobody to tell", (said, SLACK), ([], []))
        # put both back for the cases below
        store.upsert_catalog_agent("investigator", "watch-prod", "Find the cause.", budget_usd=25.0,
                                   concludes=True, slack_channel="C222")
        store.put_in_project("agent", "investigator", UID)
        store.upsert_catalog_agent("watcher", "watch-prod", "Look at prod-otlp.", budget_usd=0.05,
                                   budget_period="month", concludes=True, handoffs=HANDOFFS,
                                   slack_channel="C111")
        store.put_in_project("agent", "watcher", UID)
        store.set_wake(UID, "watch-prod", "watcher", enabled=True)
        store.set_handoffs(UID, "watcher", HANDOFFS)
        runtime.reload_catalog()

        print("== a cell that is not an AI SRE cell ==")
        real = datetime.now(UTC)
        cat = await base()
        fire(store, real - timedelta(minutes=3))
        store.set_setting(edition_mod.EDITION_SETTING, None)
        eq("no edition stored is a platform cell", edition_mod.edition(store)[0], "platform")
        step(store, cat, None)
        month = Store.month_start(real)
        spend(store, "watcher", 0.03, max(real - timedelta(minutes=20), month))
        spend(store, "watcher", 0.03, max(real - timedelta(minutes=10), month))
        changes, said = step(store, cat, None)
        eq("the stop is worked out", sorted(c["service"] for c in changes), SERVICES)
        eq("and nothing is posted", (said, SLACK), ([], []))
        r = (await cx.get("/api/watch")).json()
        eq("GET /api/watch has it", {s["service"]: s["reason"] for s in r["services"] if not s["watched"]},
           {"docs-site": "no_agent", "edge-proxy": "budget_used", "service-a": "budget_used"})
        eq("the rows stay untold", {rows()[n]["told_at"] for n in SERVICES}, {None})
        await cx.put("/api/settings/edition", json={"edition": "sre"})
        said = W.post(store, cat)
        eq("switched to sre it tells once", (len(SLACK), said),
           (1, [{"channel": "C222", "stopped": SERVICES, "started": []}]))

        print("== GET /api/watch and GET /api/services ==")
        r = (await cx.get("/api/watch")).json()
        eq("nothing is watched", r["watching"], "none")
        eq("the stopped ones first, by name", [s["service"] for s in r["services"]],
           ["docs-site", "edge-proxy", "service-a"])
        s = {x["service"]: x for x in r["services"]}["edge-proxy"]
        eq("a stopped service", (s["watched"], s["reason"], s["told"]["where"]), (False, "budget_used", "C222"))
        ck("its words", s["reason_words"].startswith("the watcher used its $0.05 budget for "), str(s))
        ck("how long", s["for_words"].startswith("for "), str(s))
        w = r["watchers"][0]
        eq("the watcher", (w["agent"], w["state"], w["reason"], w["budget_usd"], w["budget_period"]),
           ("watcher", "stopped", "budget_used", 0.05, "month"))
        eq("what it has spent", w["spent_usd"], 0.06)
        eq("every ten minutes", w["every_words"], "every 10 minutes")
        eq("it starts again on", w["resets_on"],
           Store.budget_resets_on(store.get_catalog_agent("watcher"), real).isoformat())
        for v in (s["reason_words"], s["for_words"], w["reason_words"], w["every_words"], w["spent_words"]):
            ALL_WORDS.append(v)
        items = {i["name"]: i for i in (await cx.get("/api/services")).json()["services"]}
        eq("a service says what the watch knows", (items["edge-proxy"]["watch"]["watched"],
                                                   items["edge-proxy"]["watch"]["reason"]), (False, "budget_used"))
        ck("with words", items["edge-proxy"]["watch"]["reason_words"].startswith("the watcher used"), str(items["edge-proxy"]))
        ALL_WORDS.append(items["edge-proxy"]["watch"]["reason_words"])

        print("== the daemon's loop ==")
        cat = await base()
        store.sc.execute("DELETE FROM watch_state")
        store.clear_services_cache()
        store.set_wake(UID, "watch-prod", "watcher", enabled=False)
        os.environ["TARES_WATCH_SECONDS"] = "0.05"
        stop = asyncio.Event()
        task = asyncio.create_task(W.run(runtime, stop))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if SLACK:
                break
        await asyncio.sleep(0.4)
        stop.set()
        await asyncio.wait_for(task, 5)
        os.environ["TARES_WATCH_SECONDS"] = "3600"
        eq("the loop looks, finds the watcher off and tells once", (len(SLACK), SLACK[0]["channel"] if SLACK else None),
           (1, "C222"))
        ck("with the reason", SLACK and "the watcher is turned off." in lines(SLACK[0]), str(SLACK))

        print("== the words ==")
        for message in SLACK:
            ALL_WORDS.append(json.dumps(message["blocks"]))
        for reason in W.REASONS:
            agent = store.get_catalog_agent("investigator")
            ALL_WORDS.append(W.reason_words(reason, agent, store, NOW, "watch-prod"))
        bad = {text: words.offenders(text) for text in ALL_WORDS if words.offenders(text)}
        eq("no forbidden word, no em dash", bad, {})
        ck("the list was not empty", len(ALL_WORDS) > 15, str(len(ALL_WORDS)))


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
