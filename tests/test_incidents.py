"""Incidents with an owner and an end (TR-744).

Every note the investigator writes belongs to an incident: a run of notes about one service.
Covers what opens one (and what does not), how a later note joins it inside the handoff's
cooldown, the Slack thread for a joined note on an AI SRE cell (and none on a platform cell), the
service a path or host lands on, the three routes (on_it, resolve, reopen) and their scopes, the
list filters, the sweep that closes what resolved itself, the backfill of old notes, and a finding
an outside agent records.

The model loop is a stand-in that concludes from a script per (agent, key) with the real
parse_conclude; the run path, the incident hook, the Slack post and the routes are the real ones.
Time is the module's own clock (incidents.clock), moved by hand, so nothing sleeps.

Run: .venv/bin/python tests/test_incidents.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

SLACK_PORT = 8868
TOKEN = "root-token"
TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
os.environ["TARES_SLACK_API_BASE"] = f"http://127.0.0.1:{SLACK_PORT}"
os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-test"
os.environ["TARES_PUBLIC_URL"] = "https://cell.example.com"
os.environ["TARES_AUTH_TOKEN"] = TOKEN   # read when the daemon is imported

import httpx  # noqa: E402

from fixtures.sre_cell import build  # noqa: E402
from tares import edition as edition_mod  # noqa: E402
from tares import incidents  # noqa: E402
from tares.builtin_agents import AgentRunner, parse_conclude  # noqa: E402
from tares.config import catalog_from_db  # noqa: E402
from tares.envelope import Envelope, now_utc  # noqa: E402
from tares.store import Store  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


# (agent, run key) -> the arguments of its conclude call
SCRIPT: dict = {}


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    args = SCRIPT.get((agent["name"], key), {"outcome": "no_op", "summary": ""})
    got, err = parse_conclude(args)
    assert got is not None, err
    concluded.update(got)
    return got["summary"], 1, 1, [], False, ""


AgentRunner._loop = fake_loop

SLACK: list = []   # what the stub Slack API was sent


class Slack(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SLACK.append(body)
        data = json.dumps({"ok": True, "channel": body.get("channel"),
                           "ts": f"1790000000.{len(SLACK):06d}"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


async def wait_for(fn, tries=300):
    for _ in range(tries):
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.03)
    return fn()


def H(tok=TOKEN):
    return {"Authorization": f"Bearer {tok}"}


def utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def seed_old_note(store, rid, key, at, project, verdict="rca"):
    """A finished investigator run from the past: what an older cell holds."""
    store.start_agent_run(rid, "investigator", "watch-prod", "", key, "h", woken_by="handoff",
                          project=project)
    store.finish_agent_run(rid, "ok", finding=f"{key} returns errors since the last deploy.",
                           outcome="finding", verdict=verdict)
    with store._swrite:
        store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, rid])


# ── the cell ─────────────────────────────────────────────────────────────────
async def cell():
    from tares.daemon import make_app
    threading.Thread(target=HTTPServer(("127.0.0.1", SLACK_PORT), Slack).serve_forever,
                     daemon=True).start()
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H())
        store, runtime, runner = app.state.store, app.state.runtime, app.state.agents
        runner.attach_loop()
        base = datetime.now(timezone.utc).replace(microsecond=0)
        clock = [base]
        incidents.clock = lambda: clock[0]

        r = await cx.put("/api/settings/edition", json={"edition": "sre"})
        eq("edition set to AI SRE", r.status_code, 200)
        made = build(store, base, watcher=True, investigator=True)
        uid = made["project"]
        store.set_handoffs(uid, "watcher", [{"verdict": "investigate", "agent": "investigator",
                                             "cooldown": "30m"}])
        store.upsert_catalog_agent(
            "investigator", "watch-prod", "Find the cause of what the watcher flagged.",
            budget_usd=25.0, concludes=True, slack_channel="C1")
        runtime.reload_catalog()

        async def finish(rid):
            row = await wait_for(lambda: (lambda x: x if x and x["status"] != "running" else None)(
                store.get_agent_run(rid)))
            return row

        async def investigate(run_key, parent=None, **args):
            """The investigator on `run_key`, started by a handoff of `parent`. Waits for it."""
            SCRIPT[("investigator", run_key)] = args
            rid = runner.run_now("investigator", "watch-prod", run_key, "", woken_by="handoff",
                                 parent_run_id=parent, project=uid)
            row = await finish(rid)
            assert row and row["status"] == "ok", row
            return rid

        async def active(**q):
            r = await cx.get("/api/incidents", params={"state": "active", **q})
            assert r.status_code == 200, r.text
            return r.json()

        async def detail(iid):
            r = await cx.get(f"/api/incidents/{iid}")
            assert r.status_code == 200, r.text
            return r.json()

        print("== the watcher's finding opens no incident ==")
        SCRIPT[("watcher", "edge-proxy")] = {"outcome": "finding", "verdict": "investigate",
                                             "summary": "502s on the edge-proxy rose to 12x."}
        w1 = runner.run_now("watcher", "watch-prod", "edge-proxy", "", project=uid)
        await finish(w1)
        await asyncio.sleep(0.3)   # a handoff, if one started, has had time to finish
        got = await active()
        eq("no incident after a watcher finding", got["incidents"], [])
        eq("the counts start at zero", got["counts"], {"open": 0, "on_it": 0, "resolved": 0})
        eq("a missing incident is a 404", (await cx.post("/api/incidents/nope/on_it")).status_code,
           404)

        print("== the investigator's note opens one ==")
        n_slack = len(SLACK)
        n1 = await investigate("edge-proxy", w1, outcome="finding", verdict="rca",
                               summary="The 14:02 deploy of service-a left the proxy on old pods.")
        got = await active()
        eq("one incident", len(got["incidents"]), 1)
        inc1 = got["incidents"][0]
        eq("it is open, on edge-proxy, with one note",
           (inc1["state"], inc1["service"], inc1["notes"]), ("open", "edge-proxy", 1))
        eq("one Slack message went out, no thread", [m.get("thread_ts") for m in SLACK[n_slack:]],
           [None])
        first_ts = f"1790000000.{n_slack + 1:06d}"
        eq("the incident keeps the channel and ts", inc1["slack"],
           {"channel": "C1", "ts": first_ts})
        d1 = await detail(inc1["id"])
        eq("so does the note", d1["notes"][0]["slack"], {"channel": "C1", "ts": first_ts})
        eq("the log says it opened", [e["action"] for e in d1["log"]], ["opened"])
        ck("it reads in words", d1["state_words"].startswith("Open for") and "nobody" in d1["state_words"],
           d1["state_words"])
        ck("the title names the service", d1["title"].startswith("edge-proxy"), d1["title"])

        print("== a second note ten minutes later joins it ==")
        clock[0] = base + timedelta(minutes=10)
        n_slack = len(SLACK)
        n2 = await investigate("edge-proxy", w1, outcome="finding", verdict="rca",
                               summary="The proxy still points at pods the deploy removed.")
        got = await active()
        eq("still one incident", len(got["incidents"]), 1)
        eq("with two notes", got["incidents"][0]["notes"], 2)
        eq("the Slack post is a reply under the first message",
           [m.get("thread_ts") for m in SLACK[n_slack:]], [first_ts])
        d1 = await detail(inc1["id"])
        eq("its own ts is stored on the second note", d1["notes"][1]["slack"],
           {"channel": "C1", "ts": f"1790000000.{n_slack + 1:06d}"})
        eq("the incident still points at the first message", d1["slack"]["ts"], first_ts)
        eq("the log says a note joined", [e["action"] for e in d1["log"]], ["opened", "note_joined"])

        print("== a resolved finding is no note ==")
        n_slack = len(SLACK)
        await investigate("edge-proxy", w1, outcome="finding", verdict="resolved",
                          summary="The spike is over. Nothing to do.")
        eq("still one incident with two notes",
           [(i["notes"]) for i in (await active())["incidents"]], [2])
        eq("and nothing joined it", len((await detail(inc1["id"]))["notes"]), 2)

        print("== a note after the cooldown opens a second incident ==")
        clock[0] = base + timedelta(minutes=50)   # 40 minutes after the last note, cooldown 30m
        n3 = await investigate("edge-proxy", w1, outcome="finding", verdict="rca",
                               summary="The 502s are back after a second deploy.")
        got = await active(service="edge-proxy")
        eq("two incidents on edge-proxy", len(got["incidents"]), 2)
        inc2 = next(i for i in got["incidents"] if i["id"] != inc1["id"])
        eq("the new one has the new note only", inc2["notes"], 1)
        eq("newest first", got["incidents"][0]["id"], inc2["id"])
        eq("a note after the cooldown is a message of its own", SLACK[-1].get("thread_ts"), None)

        print("== a path lands on the service that carries it ==")
        t = datetime.now(timezone.utc)
        store.append([Envelope(source="prod-otlp", source_type="otlp", key_value="service-a",
                               event_type="span", text="POST /jobs", event_time=t, payload={},
                               labels={"service": "service-a", "path": "/jobs"}, ingest_time=t)])
        clock[0] = base + timedelta(minutes=55)
        n4 = await investigate("service-a", w1, outcome="finding", verdict="rca", key="/jobs",
                               label="path", summary="Posting jobs fails with a timeout.")
        got = await active(service="service-a")
        eq("the incident is on service-a, not on /jobs",
           [(i["service"], i["notes"]) for i in got["incidents"]], [("service-a", 1)])
        inc3 = got["incidents"][0]
        eq("no incident is named after the path",
           [i for i in (await active())["incidents"] if i["service"] == "/jobs"], [])

        print("== people take, close and reopen it ==")
        key = (await cx.post("/api/keys", json={"name": "viewer", "scopes": ["read"]})).json()
        read = H(key["secret"])
        eq("a read key may list", (await cx.get("/api/incidents", headers=read)).status_code, 200)
        eq("a read key may not take one",
           (await cx.post(f"/api/incidents/{inc1['id']}/on_it", headers=read)).status_code, 403)
        eq("so it is still open", (await detail(inc1["id"]))["state"], "open")
        r = await cx.post(f"/api/incidents/{inc1['id']}/on_it")
        eq("the admin credential may", r.status_code, 200)
        who = r.json()["who"]["name"]
        eq("it is on_it, by the credential's name", (r.json()["state"], who),
           ("on_it", "auth token (env)"))
        ck("it reads in words", r.json()["state_words"].startswith(f"{who} has been on it"),
           r.json()["state_words"])
        r = await cx.post(f"/api/incidents/{inc2['id']}/resolve")
        eq("resolve answers", r.status_code, 200)
        out = r.json()
        eq("it is resolved by the credential, as a person",
           (out["state"], out["resolved"]["by_name"], out["resolved"]["how"]),
           ("resolved", "auth token (env)", "person"))
        handled = store.get_agent_run(n3)
        eq("its note is marked handled by them", handled["handled_by"], "auth token (env)")
        eq("a note of another incident is not", store.get_agent_run(n1)["handled_by"], None)
        r = await cx.post(f"/api/incidents/{inc2['id']}/reopen")
        eq("reopen sets it open", (r.json()["state"], r.json()["who"]), ("open", None))

        print("== the list filters ==")
        got = await active(service="edge-proxy")
        eq("active and edge-proxy: both, one on_it one open",
           sorted((i["state"], i["service"]) for i in got["incidents"]),
           [("on_it", "edge-proxy"), ("open", "edge-proxy")])
        eq("active and service-a: the one", [i["id"] for i in (await active(service="service-a"))["incidents"]],
           [inc3["id"]])
        r = await cx.get("/api/incidents", params={"state": "on_it"})
        eq("on_it alone", [i["id"] for i in r.json()["incidents"]], [inc1["id"]])
        r = await cx.get("/api/incidents", params={"state": "resolved"})
        eq("nothing is resolved yet", r.json()["incidents"], [])
        eq("a state that does not exist is refused",
           (await cx.get("/api/incidents", params={"state": "sleeping"})).status_code, 400)
        eq("the counts follow", (await active())["counts"], {"open": 2, "on_it": 1, "resolved": 0})

        print("== the sweep closes what resolved itself ==")
        clock[0] = base - timedelta(hours=30)
        await investigate("sweep-a", w1, outcome="finding", verdict="rca",
                          summary="sweep-a returns errors after a config change.")
        await investigate("sweep-b", w1, outcome="finding", verdict="rca",
                          summary="sweep-b returns errors after a config change.")
        clock[0] = base
        await investigate("sweep-a", w1, outcome="finding", verdict="resolved",
                          summary="The errors on sweep-a stopped. Nothing to do.")
        await investigate("sweep-b", w1, outcome="finding", verdict="resolved",
                          summary="The errors on sweep-b stopped. Nothing to do.")
        olds = {i["service"]: i for i in (await active())["incidents"]
                if i["service"].startswith("sweep-")}
        eq("both old incidents are open", sorted((s, i["state"]) for s, i in olds.items()),
           [("sweep-a", "open"), ("sweep-b", "open")])
        r = await cx.post(f"/api/incidents/{olds['sweep-b']['id']}/on_it")
        eq("somebody takes sweep-b", r.json()["state"], "on_it")
        incidents.sweep(store, runtime.catalog, now=base)
        a = await detail(olds["sweep-a"]["id"])
        eq("sweep-a closed itself", (a["state"], a["resolved"]["how"], a["resolved"]["by_name"]),
           ("resolved", "itself", None))
        ck("in words", a["state_words"].startswith("Resolved itself after"), a["state_words"])
        eq("sweep-b is left alone", (await detail(olds["sweep-b"]["id"]))["state"], "on_it")
        eq("so are the fresh ones",
           sorted(i["state"] for i in (await active(service="edge-proxy"))["incidents"]),
           ["on_it", "open"])
        eq("the notes of a self-resolved incident are not marked handled",
           store.get_agent_run(a["notes"][0]["run_id"])["handled_by"], None)

        print("== a finding an outside agent records opens one too ==")
        r = await cx.post(f"/api/projects/{uid}/findings",
                          json={"entity": "ext-svc", "verdict": "rca", "agent": "Alex",
                                "finding": "ext-svc leaks connections since the last release."})
        eq("recorded", r.status_code, 201)
        got = await active(service="ext-svc")
        eq("an incident on ext-svc with its note", [(i["state"], i["notes"]) for i in got["incidents"]],
           [("open", 1)])
        eq("it is the recorded run's", store.incident_for_run(r.json()["run_id"])["id"],
           got["incidents"][0]["id"])

        print("== a practice finding from an outside agent opens no incident ==")
        pk = (await cx.post("/api/keys", json={"name": "outside", "project": uid})).json()
        pkey = {"Authorization": f"Bearer {pk['secret']}"}

        def practice_firing():
            store.set_project_setup(uid, {**(store.get_project_setup(uid) or {}),
                                          "practice_kind": "own", "practice_finding": None,
                                          "practice_at": now_utc().isoformat()})

        practice_firing()
        r = await cx.post(f"/api/projects/{uid}/findings", headers=pkey,
                          json={"entity": "prac-svc", "verdict": "rca",
                                "finding": "prac-svc fails its example check."})
        eq("the practice finding is recorded", r.status_code, 201)
        eq("flagged practice", store.get_agent_run(r.json()["run_id"])["practice"], True)
        eq("it filed no note", store.incident_for_run(r.json()["run_id"]), None)
        eq("so no incident opened", (await active(service="prac-svc"))["incidents"], [])
        r = await cx.post(f"/api/projects/{uid}/findings", headers=pkey,
                          json={"entity": "prac-svc", "verdict": "rca",
                                "finding": "prac-svc really fails its health check."})
        eq("the next finding is a real one", store.get_agent_run(r.json()["run_id"])["practice"],
           False)
        got = await active(service="prac-svc")
        eq("it opens one, from itself", [(i["notes"]) for i in got["incidents"]], [1])
        eq("that note is the real run's", store.incident_for_run(r.json()["run_id"])["id"],
           got["incidents"][0]["id"])
        practice_firing()
        r = await cx.post(f"/api/projects/{uid}/findings", headers=pkey,
                          json={"entity": "prac-svc", "verdict": "rca",
                                "finding": "prac-svc fails its example check again."})
        eq("a practice finding joins none", (store.incident_for_run(r.json()["run_id"]),
                                             [i["notes"] for i in (await active(service="prac-svc"))["incidents"]]),
           (None, [1]))

        print("== a note marked handled resolves its incident ==")
        hn = await investigate("hnd-svc", w1, outcome="finding", verdict="rca",
                               summary="hnd-svc returns errors after a deploy.")
        hinc = store.incident_for_run(hn)
        eq("it is open", hinc["state"], "open")
        r = await cx.post(f"/api/projects/{uid}/results/{hn}/handled", json={"handled": True})
        eq("handled answers", r.status_code, 200)
        d = await detail(hinc["id"])
        eq("the incident is resolved by the caller",
           (d["state"], (d["resolved"] or {}).get("by_name"), (d["resolved"] or {}).get("how")),
           ("resolved", "auth token (env)", "person"))

        print("== a platform cell keeps its messages, and keeps the incidents ==")
        r = await cx.put("/api/settings/edition", json={"edition": "platform"})
        eq("edition set to platform", r.status_code, 200)
        clock[0] = base + timedelta(minutes=60)
        n_slack = len(SLACK)
        await investigate("plat-svc", w1, outcome="finding", verdict="rca",
                          summary="plat-svc fails its health check.")
        clock[0] = base + timedelta(minutes=65)
        await investigate("plat-svc", w1, outcome="finding", verdict="rca",
                          summary="plat-svc still fails its health check.")
        posts = SLACK[n_slack:]
        eq("two messages in the channel", len(posts), 2)
        eq("neither is a thread reply", [m.get("thread_ts") for m in posts], [None, None])
        got = await active(service="plat-svc")
        eq("the incident is still tracked: one, two notes",
           [(i["notes"]) for i in got["incidents"]], [2])
        d = await detail(got["incidents"][0]["id"])
        eq("each note kept where it was posted",
           [bool(n["slack"]) for n in d["notes"]], [True, True])

    incidents.clock = incidents.now_utc


# ── notes from before incidents were tracked ─────────────────────────────────
def backfill_case():
    print("== the backfill files the old notes ==")
    store = Store(os.path.join(TMP, "old.duckdb"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    made = build(store, now, watcher=True, investigator=True)
    uid = made["project"]
    eq("a new store has no incidents", store.list_incidents(), [])
    seed_old_note(store, "old1", "edge-proxy", now - timedelta(days=20), uid)
    seed_old_note(store, "old2", "edge-proxy", now - timedelta(days=20) + timedelta(minutes=10), uid)
    seed_old_note(store, "old3", "service-a", now - timedelta(days=19), uid)
    seed_old_note(store, "new1", "docs-site", now - timedelta(days=2), uid)
    seed_old_note(store, "fine", "service-a", now - timedelta(days=1), uid, verdict="resolved")

    store.set_setting(incidents.BACKFILL_SETTING, "0")
    filed = incidents.backfill(store, now=now)
    eq("four notes filed", filed, 4)
    rows = store.list_incidents(limit=100)
    by = {r["service"]: r for r in rows}
    eq("three incidents", sorted(by), ["docs-site", "edge-proxy", "service-a"])
    eq("the two edge-proxy notes joined", len(store.incident_notes(by["edge-proxy"]["id"])), 2)
    eq("the old ones are closed as from before tracking",
       [(by[s]["state"], by[s]["resolution"]) for s in ("edge-proxy", "service-a")],
       [("resolved", "before_tracking")] * 2)
    eq("a recent one is open", by["docs-site"]["state"], "open")
    eq("the flag is set", store.get_setting(incidents.BACKFILL_SETTING), incidents.BACKFILL_VERSION)
    eq("a finding that said resolved is no note", store.incident_for_run("fine"), None)

    eq("a second run adds nothing", incidents.backfill(store, now=now), 0)
    eq("still three incidents", len(store.list_incidents(limit=100)), 3)

    store.set_setting(incidents.BACKFILL_SETTING, "1")
    filed = incidents.backfill(store, now=now)
    eq("another version reruns", filed, 4)
    rows = store.list_incidents(limit=100)
    eq("without duplicates", (len(rows), sorted(r["service"] for r in rows)),
       (3, ["docs-site", "edge-proxy", "service-a"]))
    eq("each old note is in one incident",
       sorted(len(store.incident_notes(r["id"])) for r in rows), [1, 1, 2])
    store.con.close()


def rerun_with_live_incident_case():
    print("== a backfill rerun leaves a live incident its own notes ==")
    store = Store(os.path.join(TMP, "live.duckdb"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    uid = build(store, now, watcher=True, investigator=True)["project"]
    seed_old_note(store, "old1", "edge-proxy", now - timedelta(days=20), uid)
    seed_old_note(store, "old2", "edge-proxy", now - timedelta(days=3), uid)
    seed_old_note(store, "live1", "edge-proxy", now - timedelta(hours=1), uid)
    live = store.create_incident("edge-proxy", uid, now - timedelta(hours=1))
    store.add_incident_note(live["id"], "live1", now - timedelta(hours=1))
    store.log_incident(live["id"], "opened", None, "investigator", "runner",
                       now - timedelta(hours=1))
    title = incidents.view(store, store.get_incident(live["id"]))["title"]
    store.set_setting(incidents.BACKFILL_SETTING, "1")   # a rerun: the first one failed or is older
    eq("two old notes filed", incidents.backfill(store, now=now), 2)
    eq("the live incident holds its own note only",
       [n["run_id"] for n in store.incident_notes(live["id"])], ["live1"])
    eq("it is still open", store.get_incident(live["id"])["state"], "open")
    eq("its title is the one it had", incidents.view(store, store.get_incident(live["id"]))["title"],
       title)
    eq("each old note has an incident of its own",
       sorted(len(store.incident_notes(store.incident_for_run(r)["id"])) for r in ("old1", "old2")),
       [1, 1])
    eq("and they are not the live one",
       {store.incident_for_run(r)["id"] for r in ("old1", "old2")} & {live["id"]}, set())
    store.con.close()


def sweep_case():
    print("== a reopen lasts ==")
    store = Store(os.path.join(TMP, "sweep.duckdb"))
    T = datetime.now(timezone.utc).replace(microsecond=0)
    uid = build(store, T, watcher=True, investigator=True)["project"]
    catalog = catalog_from_db(store)
    clk = [T]
    incidents.clock = lambda: clk[0]

    def quiet_one(svc, note_at=None, clear_at=None):
        """An open incident whose only note is old, and the investigator's all-clear after it."""
        note_at = note_at or T - timedelta(hours=30)
        seed_old_note(store, f"n-{svc}", svc, note_at, uid)
        inc = store.create_incident(svc, uid, note_at)
        store.add_incident_note(inc["id"], f"n-{svc}", note_at)
        seed_old_note(store, f"c-{svc}", svc, clear_at or T - timedelta(hours=10), uid,
                      verdict="resolved")
        return inc["id"]

    def state(iid):
        return store.get_incident(iid)["state"]

    a = quiet_one("reop-a")
    eq("it resolves itself first", [i["id"] for i in incidents.sweep(store, catalog, now=T)], [a])
    incidents.reopen(store, a, "u1", "Kim")
    eq("the person reopened it", state(a), "open")
    incidents.sweep(store, catalog, now=T + timedelta(minutes=1))
    eq("a sweep a minute later leaves it open", state(a), "open")
    incidents.sweep(store, catalog, now=T + timedelta(hours=25))
    eq("so does one a day on, the all-clear came before the reopen", state(a), "open")
    seed_old_note(store, "c2-reop-a", "reop-a", T + timedelta(hours=2), uid, verdict="resolved")
    eq("a new all-clear after the reopen closes it",
       [i["id"] for i in incidents.sweep(store, catalog, now=T + timedelta(hours=25))], [a])

    b = quiet_one("reop-b")
    incidents.resolve(store, b, "u1", "Kim")
    incidents.reopen(store, b, "u1", "Kim")
    incidents.sweep(store, catalog, now=T + timedelta(minutes=1))
    eq("resolved by a person, then reopened: a sweep leaves it open", state(b), "open")
    seed_old_note(store, "c2-reop-b", "reop-b", T + timedelta(seconds=30), uid, verdict="resolved")
    incidents.sweep(store, catalog, now=T + timedelta(minutes=1))
    eq("an all-clear just after the reopen does not close it yet", state(b), "open")
    eq("a day after the reopen it does",
       [i["id"] for i in incidents.sweep(store, catalog, now=T + timedelta(hours=25))
        if i["id"] == b], [b])

    print("== the sweep reads past its first page ==")
    seed_old_note(store, "c-old", "f7-old", T - timedelta(hours=40), uid, verdict="resolved")
    old = store.create_incident("f7-old", uid, T - timedelta(hours=50))
    keep = [store.create_incident(f"f7-{i}", uid, T - timedelta(hours=30 + i))["id"]
            for i in range(3)]   # newer than f7-old, no all-clear: they stay open
    page = incidents.SWEEP_PAGE
    incidents.SWEEP_PAGE = 2
    try:
        closed = [i["id"] for i in incidents.sweep(store, catalog, now=T)]
    finally:
        incidents.SWEEP_PAGE = page
    eq("the oldest incident closed although newer ones fill the first page", old["id"] in closed,
       True)
    eq("the others are open", [state(i) for i in keep], ["open"] * 3)

    print("== a watcher's findings do not hide the investigator's all-clear ==")
    f6 = quiet_one("f6-svc")
    for i in range(22):   # newer than the all-clear: the 20 newest rows are all the watcher's
        rid = f"w-{i}"
        store.start_agent_run(rid, "watcher", "watch-prod", "", "f6-svc", "h", project=uid)
        store.finish_agent_run(rid, "ok", finding="f6-svc looks off.", outcome="finding",
                               verdict="investigate")
    eq("it still closes", f6 in [i["id"] for i in incidents.sweep(store, catalog, now=T)], True)
    incidents.clock = incidents.now_utc
    store.con.close()


# ── the pieces that need no cell ─────────────────────────────────────────────
def words():
    print("== how an incident reads ==")
    t = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    base = {"opened_at": t, "owner_at": None, "resolved_at": None, "owner_name": None,
            "resolved_by_name": None, "resolution": None}
    eq("open", incidents.state_words({**base, "state": "open"}, t + timedelta(minutes=38)),
       "Open for 38 minutes, nobody on it")
    eq("on it", incidents.state_words({**base, "state": "on_it", "owner_name": "Alex",
                                       "owner_at": t + timedelta(minutes=26)},
                                      t + timedelta(minutes=38)),
       "Alex has been on it for 12 minutes")
    eq("resolved by a person",
       incidents.state_words({**base, "state": "resolved", "resolution": "person",
                              "resolved_by_name": "Kim", "resolved_at": t + timedelta(hours=2)}),
       "Resolved by Kim after 2 hours")
    eq("resolved itself",
       incidents.state_words({**base, "state": "resolved", "resolution": "itself",
                              "resolved_at": t + timedelta(hours=2)}),
       "Resolved itself after 2 hours")
    eq("from before tracking",
       incidents.state_words({**base, "state": "resolved", "resolution": "before_tracking"}),
       "Closed: from before incidents were tracked")
    ck("no em dash in any of them", all(chr(0x2014) not in s for s in (
        incidents.state_words({**base, "state": "open"}, t),
        incidents.state_words({**base, "state": "resolved", "resolution": "before_tracking"}))))


if __name__ == "__main__":
    words()
    asyncio.run(cell())
    backfill_case()
    rerun_with_live_incident_case()
    sweep_case()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
