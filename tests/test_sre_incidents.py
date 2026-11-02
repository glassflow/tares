"""The AI SRE Incidents screens (TR-751): GET /api/sre/incidents, GET /api/sre/incidents/{id} and
POST /api/sre/incidents/{id}/{action}.

Covers the list (groups in order, the 20 latest resolved and the page after them, the filters, the
state words for each state including "resolved itself", the raised findings with show=all), the
page of one incident (note, evidence free of the words the edition list forbids, joined notes, the
chart, the worth answer, the actions per state), the Slack link (asked of Slack once and kept; null
when there is no message, no token or Slack is down), each action (the stored state and the screen
agree, Slack is told through the same hook as the engine routes), a read key (403 on a write),
the 404s, and no em dash anywhere.

Slack is a stub API on a local port; the model never runs: the runs are written the way an agent
leaves them.

Run: .venv/bin/python tests/test_sre_incidents.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

SLACK_PORT = 8871
TOKEN = "root-token"
TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
os.environ["TARES_SLACK_API_BASE"] = f"http://127.0.0.1:{SLACK_PORT}"
os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-test"
os.environ["TARES_AUTH_TOKEN"] = TOKEN   # read when the daemon is imported

import httpx  # noqa: E402

from fixtures.sre_cell import build  # noqa: E402
from tares import goal, incidents, words  # noqa: E402
from tares.screens import sre_incidents  # noqa: E402

P = F = 0
SKIP = ("title", "service", "services", "who", "note", "failing", "likely_cause", "next_step",
        "text", "by", "name")   # what an agent or a person wrote, or a name a person gave
DASH = chr(0x2014)   # spelled out so this file has none
PERMALINK = "https://example.slack.com/archives/C1/p1790000000000001"


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


CALLS: list = []          # (method, path, body or query) the stub Slack API was sent
STUB = {"link": True}     # False: chat.getPermalink says no


class Slack(BaseHTTPRequestHandler):
    def _send(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        CALLS.append(("POST", self.path, body))
        self._send({"ok": True, "channel": body.get("channel"), "ts": f"1790000000.{len(CALLS):06d}"})

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        CALLS.append(("GET", u.path, q))
        if u.path == "/chat.getPermalink":
            self._send({"ok": True, "permalink": PERMALINK} if STUB["link"]
                       else {"ok": False, "error": "message_not_found"})
        elif u.path == "/conversations.info":
            self._send({"ok": True, "channel": {"id": q.get("channel"), "name": "alerts"}})
        else:
            self._send({"ok": False, "error": "unknown_method"})

    def log_message(self, *a):
        pass


def got(method, path):
    return [b for m, p, b in CALLS if m == method and p == path]


async def wait_for(fn, tries=200):
    for _ in range(tries):
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.03)
    return fn()


def H(tok=TOKEN):
    return {"Authorization": f"Bearer {tok}"}


def items(screen, state):
    return next((g["items"] for g in screen["groups"] if g["state"] == state), None)


def no_forbidden(label, body):
    bad = words.offenders_in(body, SKIP)
    ck(label, not bad, str(bad[:5]))


def no_dash(label, body):
    ck(label, DASH not in json.dumps(body, ensure_ascii=False))


def plain_words():
    """The mapper turns each forbidden word into a plain one and leaves the allowed phrases."""
    p = sre_incidents.plain
    eq("trigger and run", p("The trigger started a run"), "The check rule started a check")
    eq("case is kept", p("Source Catalog"), "Connection Connections")
    eq("an allowed phrase stays", p("an open source tool"), "an open source tool")
    eq("every word on the list goes",
       words.offenders(p("source view trigger run dispatch firing delivery wiring catalog "
                         "entity ingest station sources runs")), [])
    ck("an em dash goes", DASH not in p(f"a {DASH} b"))


async def cell():
    from tares.daemon import make_app
    threading.Thread(target=HTTPServer(("127.0.0.1", SLACK_PORT), Slack).serve_forever,
                     daemon=True).start()
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=H())
        store, runtime = app.state.store, app.state.runtime
        base = datetime.now(timezone.utc).replace(microsecond=0)
        clock = [base]
        incidents.clock = lambda: clock[0]

        eq("edition set to AI SRE",
           (await cx.put("/api/settings/edition", json={"edition": "sre"})).status_code, 200)
        made = build(store, base, watcher=True, investigator=True)
        uid = made["project"]
        runtime.reload_catalog()

        def run(rid, agent, key, finding, at, parent=None, verdict="rca", status="ok",
                outcome="finding", **kw):
            store.start_agent_run(rid, agent, "watch-prod", "", key, "h",
                                  woken_by="handoff" if parent else None,
                                  parent_run_id=parent, project=uid)
            store.finish_agent_run(rid, status, finding=finding, outcome=outcome, verdict=verdict,
                                   **kw)
            with store._swrite:
                store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [at, rid])

        def backdate(iid, **cols):   # take and resolve stamp the real time, not the test clock
            with store._swrite:
                for col, at in cols.items():
                    store.sc.execute(f"UPDATE incidents SET {col} = ? WHERE id = ?", [at, iid])

        def incident(service, opened, runs, **kw):
            inc = store.create_incident(service, uid, opened, **kw)
            for rid, at in runs:
                store.add_incident_note(inc["id"], rid, at)
            return inc["id"]

        # A: open 38 minutes, two notes, a Slack message. Raised by a watcher, so it can be answered.
        t = base - timedelta(minutes=38)
        run("w-a", "watcher", "edge-proxy", "502s on edge-proxy rose to 12x.", t,
            verdict="investigate")
        run("i-a1", "investigator", "edge-proxy",
            "Let me look at the logs. The 14:02 deploy of service-a left the proxy on old "
            "pods.\n\nCause: the rollout never finished.", t + timedelta(minutes=1), parent="w-a",
            failing="edge-proxy returns 502 on /ui", likely_cause="The rollout never finished",
            next_step="Roll the deploy back")
        run("i-a2", "investigator", "edge-proxy", "The proxy still points at removed pods.",
            base - timedelta(minutes=20), parent="w-a")
        # a check a limit stopped: the console's story says "did not run"
        run("i-a3", "investigator", "edge-proxy", None, t + timedelta(minutes=2), parent="w-a",
            status="capped", outcome=None, verdict=None,
            error="the daily limit for runs was reached")
        a = incident("edge-proxy", t, [("i-a1", t), ("i-a2", base - timedelta(minutes=20))])
        store.set_note_slack("i-a1", "C1", "1790000000.000001")
        store.set_incident_slack(a, "C1", "1790000000.000001")

        # G: open 5 minutes, newest, no Slack message, a note nobody raised (cannot be answered).
        t = base - timedelta(minutes=5)
        run("i-g", "investigator", "service-a", "Jobs fail since 14:20.", t, verdict="rca",
            outcome="no_op")
        g = incident("service-a", t, [("i-g", t)])

        # B: Alex took it 20 minutes ago.
        t = base - timedelta(minutes=60)
        run("i-b", "investigator", "docs-site", "Builds of docs-site fail.", t)
        b = incident("docs-site", t, [("i-b", t)])
        incidents.take(store, b, "alex", "Alex", "console")
        backdate(b, owner_at=base - timedelta(minutes=20))

        # C: resolved by Alex two days ago.
        t = base - timedelta(days=2, hours=1)
        run("i-c", "investigator", "service-a-receiver", "The receiver stopped.", t)
        c = incident("service-a-receiver", t, [("i-c", t)])
        incidents.resolve(store, c, "alex", "Alex", "console")
        backdate(c, resolved_at=base - timedelta(days=2))

        # D: resolved itself after 12 minutes; E: from before incidents were tracked.
        t = base - timedelta(hours=5)
        d = incident("edge-proxy", t, [], state="resolved", resolved_at=t + timedelta(minutes=12),
                     resolution="itself")
        t = base - timedelta(days=9)
        e = incident("edge-proxy", t, [], state="resolved", resolved_at=t + timedelta(hours=1),
                     resolution="before_tracking")
        # 22 more resolved ones: 25 in all, the page holds 20
        for i in range(22):
            t = base - timedelta(days=3, minutes=i * 7)
            incident("service-a", t, [], state="resolved", resolved_at=t + timedelta(minutes=3),
                     resolution="itself")

        # a finding a watcher raised that did not become an incident
        run("w-r", "watcher", "docs-site", "docs-site builds are slow.", base - timedelta(minutes=3),
            verdict="investigate")

        print("== the list ==")
        r = await cx.get("/api/sre/incidents")
        eq("200", r.status_code, 200)
        s = r.json()
        eq("keys", sorted(s), ["counts", "groups", "next_before", "quiet_checks_words", "services"])
        eq("groups in order", [g["state"] for g in s["groups"]], ["open", "on_it", "resolved"])
        eq("open, newest note first", [i["id"] for i in items(s, "open")], [g, a])
        eq("on it", [i["id"] for i in items(s, "on_it")], [b])
        res = items(s, "resolved")
        eq("the 20 latest resolved", len(res), 20)
        eq("resolved, newest note first", [i["id"] for i in res[:3]], [d, c, res[2]["id"]])
        eq("counts", s["counts"], {"open": 2, "on_it": 1, "resolved": 25})
        eq("services for the filter", s["services"],
           ["docs-site", "edge-proxy", "service-a", "service-a-receiver"])
        ck("more resolved: a page marker", bool(s["next_before"]), str(s["next_before"]))
        by_id = {i["id"]: i for g_ in s["groups"] for i in g_["items"]}
        eq("an open incident", by_id[a]["state_words"], "open 38 minutes, nobody on it")
        eq("an open incident's who", by_id[a]["who"], None)
        eq("someone on it", by_id[b]["state_words"],
           f"Alex is on it since {goal._since_words(base - timedelta(minutes=20), base)}")
        eq("someone on it, who", by_id[b]["who"], "Alex")
        eq("resolved by a person", by_id[c]["state_words"], "resolved by Alex, 2 days ago")
        eq("resolved by a person, who", by_id[c]["who"], "Alex")
        eq("resolved itself", by_id[d]["state_words"], "resolved itself after 12 minutes")
        eq("resolved itself, who", by_id[d]["who"], None)
        ck("an item has its keys", sorted(by_id[a]) == sorted(
            ["id", "incident", "title", "service", "state", "state_words", "opened_at",
             "when_words", "who"]), str(sorted(by_id[a])))
        eq("when words", by_id[a]["when_words"], goal.age_words(38 * 60))
        ck("the title names the service", "edge-proxy" in by_id[a]["title"], by_id[a]["title"])
        no_forbidden("no forbidden word in the list", s)
        no_dash("no em dash in the list", s)

        print("== paging, filters, show=all ==")
        p2 = (await cx.get("/api/sre/incidents", params={"before": s["next_before"]})).json()
        eq("the next page holds only resolved", [g["state"] for g in p2["groups"]], ["resolved"])
        eq("the rest of the resolved", len(items(p2, "resolved")), 5)
        eq("and no more pages", p2["next_before"], None)
        ck("none of them repeats", not ({i["id"] for i in res} & {i["id"] for i in items(p2, "resolved")}))
        one = (await cx.get("/api/sre/incidents", params={"state": "on_it"})).json()
        eq("one state, one group", [g["state"] for g in one["groups"]], ["on_it"])
        flt = (await cx.get("/api/sre/incidents", params={"service": "edge-proxy"})).json()
        eq("a service filter", sorted(i["id"] for g_ in flt["groups"] for i in g_["items"]),
           sorted([a, d, e]))
        eq("counts follow the filter", flt["counts"], {"open": 1, "on_it": 0, "resolved": 2})
        eq("a bad state is refused", (await cx.get("/api/sre/incidents",
                                                   params={"state": "x"})).status_code, 400)
        eq("a bad time is refused", (await cx.get("/api/sre/incidents",
                                                  params={"before": "soon"})).status_code, 400)
        allv = (await cx.get("/api/sre/incidents", params={"show": "all"})).json()
        raised = items(allv, "raised")
        ck("show=all adds the raised group", raised is not None, str([g_["state"] for g_ in allv["groups"]]))
        eq("it holds the finding that is no incident", [i["id"] for i in raised or []], ["w-r"])
        eq("and says so", [i["state_words"] for i in raised or []], ["raised, not an incident"])
        eq("without show=all there is no such group", items(s, "raised"), None)
        no_forbidden("no forbidden word with show=all", allv)

        print("== one incident ==")
        r = await cx.get(f"/api/sre/incidents/{a}")
        eq("200", r.status_code, 200)
        d_ = r.json()
        eq("keys", sorted(d_), sorted([
            "id", "title", "service", "state", "state_words", "opened_at", "who", "resolved",
            "failing", "likely_cause", "next_step", "note", "evidence", "chart", "slack", "joined",
            "worth", "actions"]))
        eq("state", d_["state"], "open")
        eq("state words", d_["state_words"], "open 38 minutes, nobody on it")
        eq("who and resolved are empty", (d_["who"], d_["resolved"]), (None, None))
        eq("failing", d_["failing"], "edge-proxy returns 502 on /ui")
        eq("likely cause", d_["likely_cause"], "The rollout never finished")
        eq("next step", d_["next_step"], "Roll the deploy back")
        ck("the note is the finding, filler stripped", d_["note"].startswith("The 14:02 deploy")
           and "Summary:" not in d_["note"], d_["note"])
        ck("evidence is a list of at/text", d_["evidence"] and all(
            sorted(x) == ["at", "text"] for x in d_["evidence"]), str(d_["evidence"]))
        raw = goal.result_detail(store, runtime.catalog, uid, "i-a1", set())["steps"]
        ck("the steps the console shows say a word the edition list forbids",
           any(words.offenders(x["text"]) for x in raw), str([x["text"] for x in raw]))
        eq("the evidence says none", [x for x in d_["evidence"] if words.offenders(x["text"])], [])
        eq("the same number of steps", len(d_["evidence"]), len(raw))
        eq("the joined note", [j["at"] for j in d_["joined"]],
           [goal._iso(store.incident_notes(a)[1]["joined_at"])])
        ck("the joined note has a title", "edge-proxy" in d_["joined"][0]["title"],
           d_["joined"][0]["title"])
        eq("the actions of an open incident", d_["actions"], ["on_it", "resolve"])
        eq("the chart holds errors", d_["chart"]["label"].startswith("Errors from edge-proxy"), True)
        ck("the chart has the 502", sum(n for _t, n in d_["chart"]["points"]) >= 1,
           str(d_["chart"]["points"]))
        ck("the chart points are [time, count]", all(
            isinstance(p_[0], str) and isinstance(p_[1], int) for p_ in d_["chart"]["points"]))
        eq("deploys is a list", isinstance(d_["chart"]["deploys"], list), True)
        eq("nobody answered, and a person can", d_["worth"],
           {"answer": None, "by": None, "can_answer": True})
        no_forbidden("no forbidden word in the page", d_)
        no_dash("no em dash in the page", d_)
        eq("someone on it: who", (await cx.get(f"/api/sre/incidents/{b}")).json()["who"]["name"],
           "Alex")
        eq("someone on it: actions", (await cx.get(f"/api/sre/incidents/{b}")).json()["actions"],
           ["resolve", "reopen"])
        rc = (await cx.get(f"/api/sre/incidents/{c}")).json()
        eq("resolved by Alex", (rc["resolved"]["by"], rc["resolved"]["words"]),
           ("Alex", "Resolved by Alex"))
        eq("resolved: actions", rc["actions"], ["reopen"])
        eq("resolved: state words", rc["state_words"], "resolved by Alex, 2 days ago")
        rd = (await cx.get(f"/api/sre/incidents/{d}")).json()
        eq("resolved itself", (rd["resolved"]["by"], rd["resolved"]["words"], rd["state_words"]),
           (None, "Resolved itself after 12 minutes", "resolved itself after 12 minutes"))
        eq("an incident with no notes still answers", (rd["note"], rd["evidence"], rd["joined"]),
           ("", [], []))
        re_ = (await cx.get(f"/api/sre/incidents/{e}")).json()
        eq("from before tracking", re_["state_words"], "closed, from before incidents were tracked")
        eq("a note nobody raised cannot be answered",
           (await cx.get(f"/api/sre/incidents/{g}")).json()["worth"]["can_answer"], False)

        print("== the Slack link ==")
        eq("an incident with no message", (await cx.get(f"/api/sre/incidents/{g}")).json()["slack"],
           None)
        eq("the link is asked of Slack on the first read", len(got("GET", "/chat.getPermalink")), 1)
        eq("with the channel and message", got("GET", "/chat.getPermalink")[0],
           {"channel": "C1", "message_ts": "1790000000.000001"})
        eq("the page has it", d_["slack"], {"where": "#alerts", "url": PERMALINK})
        eq("and it is kept", store.get_incident(a)["slack_url"], PERMALINK)
        again = (await cx.get(f"/api/sre/incidents/{a}")).json()
        eq("the second read does not ask again", len(got("GET", "/chat.getPermalink")), 1)
        eq("and shows the same link", again["slack"]["url"], PERMALINK)
        # a note posted before the incident kept its message: the link comes from the note
        run("i-h", "investigator", "docs-site", "Deploys of docs-site stall.",
            base - timedelta(minutes=9))
        h_ = incident("docs-site", base - timedelta(minutes=9), [("i-h", base - timedelta(minutes=9))])
        store.set_note_slack("i-h", "C2", "1790000000.000009")
        eq("a note's message gives the link", (await cx.get(f"/api/sre/incidents/{h_}")).json()["slack"]["url"],
           PERMALINK)
        # Slack says no: the page still answers, the link is null
        STUB["link"] = False
        run("i-j", "investigator", "docs-site", "Another.", base - timedelta(minutes=8))
        j = incident("docs-site", base - timedelta(minutes=8), [("i-j", base - timedelta(minutes=8))])
        store.set_note_slack("i-j", "C3", "1790000000.000010")
        rj = await cx.get(f"/api/sre/incidents/{j}")
        eq("Slack says no: the page answers", rj.status_code, 200)
        eq("and the link is null", rj.json()["slack"]["url"], None)
        eq("the place is still named", rj.json()["slack"]["where"], "#alerts")
        eq("nothing is kept", store.get_incident(j)["slack_url"], None)
        # Slack unreachable
        old_base = sre_incidents.slack_mod.API_BASE
        sre_incidents.slack_mod.API_BASE = "http://127.0.0.1:1"
        sre_incidents._slack_failed.clear()
        sre_incidents._channel_names.clear()
        store.set_incident_slack_url(a, None)
        ru = await cx.get(f"/api/sre/incidents/{a}")
        eq("Slack unreachable: the page answers", ru.status_code, 200)
        eq("and the link is null", (ru.json()["slack"]["url"], ru.json()["slack"]["where"]),
           (None, "Slack"))
        sre_incidents.slack_mod.API_BASE = old_base
        # no token
        os.environ.pop("TARES_SLACK_BOT_TOKEN")
        sre_incidents._slack_failed.clear()
        rn = await cx.get(f"/api/sre/incidents/{a}")
        eq("no token: the page answers", rn.status_code, 200)
        eq("and the link is null", rn.json()["slack"]["url"], None)
        os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-test"
        STUB["link"] = True
        sre_incidents._slack_failed.clear()
        eq("asked again later, it is there",
           (await cx.get(f"/api/sre/incidents/{a}")).json()["slack"]["url"], PERMALINK)
        # a new message drops the kept link
        store.set_incident_slack(a, "C1", "1790000000.000077")
        eq("a new message has no kept link", store.get_incident(a)["slack_url"], None)
        store.set_incident_slack(a, "C1", "1790000000.000001")

        print("== the actions ==")
        n0 = len(got("POST", "/chat.update"))
        r = await cx.post(f"/api/sre/incidents/{a}/on_it")
        eq("on_it: 200", r.status_code, 200)
        s = r.json()
        eq("on_it: the stored state", store.get_incident(a)["state"], "on_it")
        eq("on_it: the screen", s["state"], "on_it")
        ck("on_it: the words", " is on it since " in s["state_words"], s["state_words"])
        eq("on_it: who", bool(s["who"] and s["who"]["name"]), True)
        eq("on_it: actions", s["actions"], ["resolve", "reopen"])
        ck("Slack is told: the message is updated", bool(await wait_for(
            lambda: len(got("POST", "/chat.update")) > n0)))
        eq("the list agrees", a in [i["id"] for i in items(
            (await cx.get("/api/sre/incidents")).json(), "on_it")], True)
        s = (await cx.post(f"/api/sre/incidents/{a}/resolve")).json()
        eq("resolve: the stored state", store.get_incident(a)["state"], "resolved")
        eq("resolve: the stored way", store.get_incident(a)["resolution"], "person")
        eq("resolve: the screen", (s["state"], s["actions"]), ("resolved", ["reopen"]))
        ck("resolve: resolved by", s["resolved"]["words"].startswith("Resolved by "), s["resolved"]["words"])
        s = (await cx.post(f"/api/sre/incidents/{a}/reopen")).json()
        eq("reopen: the stored state", store.get_incident(a)["state"], "open")
        eq("reopen: nobody on it", store.get_incident(a)["owner"], None)
        eq("reopen: the screen", (s["state"], s["actions"], s["who"], s["resolved"]),
           ("open", ["on_it", "resolve"], None, None))
        eq("the log has the three changes", len(store.incident_log(a)), 3)
        eq("done through the console", {x["via"] for x in store.incident_log(a)[-3:]}, {"console"})
        eq("the Slack hook ran for each change", len(got("POST", "/chat.update")) >= n0 + 1, True)
        s = (await cx.post(f"/api/sre/incidents/{a}/worth_yes")).json()
        eq("worth_yes: the screen", s["worth"]["answer"], True)
        ck("worth_yes: by someone", bool(s["worth"]["by"]), str(s["worth"]))
        eq("worth_yes: stored", [r_["worth"] for r_ in store.feedback_for(["i-a1"])["i-a1"]], [True])
        s = (await cx.post(f"/api/sre/incidents/{a}/worth_no")).json()
        eq("worth_no: the screen", s["worth"]["answer"], False)
        eq("worth_no: replaces the answer", [r_["worth"] for r_ in store.feedback_for(["i-a1"])["i-a1"]],
           [False])
        eq("a note nobody raised cannot be answered (409)",
           (await cx.post(f"/api/sre/incidents/{g}/worth_yes")).status_code, 409)

        print("== refused ==")
        r = await cx.post("/api/keys", json={"name": "viewer", "scopes": ["read"]})
        rk = r.json()["secret"]
        n_log = len(store.incident_log(g))
        for act in ("on_it", "resolve", "reopen", "worth_yes", "worth_no"):
            rr = await cx.post(f"/api/sre/incidents/{g}/{act}", headers=H(rk))
            eq(f"a read key cannot {act}", rr.status_code, 403)
        eq("and nothing changed", (store.get_incident(g)["state"], len(store.incident_log(g))),
           ("open", n_log))
        eq("a read key can read the list", (await cx.get("/api/sre/incidents", headers=H(rk))).status_code, 200)
        eq("a read key can read a page", (await cx.get(f"/api/sre/incidents/{g}", headers=H(rk))).status_code, 200)
        r = await cx.get("/api/sre/incidents/inc_nope")
        eq("an unknown incident: 404", (r.status_code, r.json()["detail"]),
           (404, "No incident with that id"))
        eq("an unknown incident, an action: 404", (await cx.post("/api/sre/incidents/inc_nope/resolve")).status_code, 404)
        eq("an unknown action: 404", (await cx.post(f"/api/sre/incidents/{g}/explode")).status_code, 404)
        eq("an unknown action does nothing", store.get_incident(g)["state"], "open")
        eq("no one signs in: 401", (await httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t").get("/api/sre/incidents")).status_code, 401)

        print("== the words check ==")
        for path in ("/api/sre/incidents", f"/api/sre/incidents/{a}", f"/api/sre/incidents/{c}"):
            body = (await cx.get(path)).json()
            no_forbidden(f"{path}: no forbidden word", body)
            no_dash(f"{path}: no em dash", body)

        await cx.aclose()
        incidents.clock = incidents.now_utc


plain_words()
asyncio.run(cell())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
