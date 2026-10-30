"""Findings say what broke, not what the agent thought (TR-745).

Covers the four fields a conclude call carries (what_changed, since, failing, likely_cause), the
derivation for older notes (filler, the three parts of a numbered note, the next step), the title
("service: what changed since when") for today, yesterday and older dates, the outcome of a run
over a watcher to investigator chain, the results feed with show=worth and its quiet roll-up,
GET /api/findings across projects, the Slack header on an AI SRE cell against any other edition,
and twenty older notes that open on narration.

The model loop is a stand-in that concludes from a script per (agent, key) and builds the conclude
result with the real parse_conclude, so the real run path stores the new fields.

Run: .venv/bin/python tests/test_finding_titles.py
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

SLACK_PORT = 8867
TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
os.environ["TARES_SLACK_API_BASE"] = f"http://127.0.0.1:{SLACK_PORT}"
os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-test"
os.environ["TARES_PUBLIC_URL"] = "https://cell.example.com"

import httpx  # noqa: E402

from fixtures.sre_cell import build  # noqa: E402
from tares import edition as edition_mod  # noqa: E402
from tares import goal as G  # noqa: E402
from tares.builtin_agents import AgentRunner, parse_conclude  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


# (agent, key) -> the arguments of its conclude call
SCRIPT: dict = {}


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    args = SCRIPT.get((agent["name"], key), {"outcome": "no_op", "summary": ""})
    got, err = parse_conclude(args)
    assert got is not None, err
    concluded.update(got)
    return got["summary"], 1, 1, [], False, ""


AgentRunner._loop = fake_loop

SLACK = []   # what the stub Slack API was sent


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


# a note the way the investigator writes it: narration, then three numbered parts
NOTE = ("I now have the whole picture.\n\n"
        "## 1. What is failing\n\nThe edge-proxy returns 502s for /ui since 14:05, about 12x the "
        "usual rate.\n\n"
        "## 2. Most likely cause\n\nThe 14:02 deploy of service-a restarted its pods and the "
        "proxy still points at the old ones.\n\n"
        "## 3. Suggested next action\n\nRoll service-a back to the previous release and watch "
        "the edge-proxy 5xx rate for ten minutes.")


async def wait_for(fn, tries=300):
    for _ in range(tries):
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.03)
    return fn()


def settled(store, agent, n):
    rs = list(reversed(store.list_agent_runs(agent, limit=100)))
    return rs if len(rs) >= n and all(r["status"] != "running" for r in rs) else None


# ── without the daemon ───────────────────────────────────────────────────────
def pure():
    print("== conclude keeps the four fields ==")
    now = datetime.now(timezone.utc)
    past = (now - timedelta(minutes=40)).replace(microsecond=0)
    got, err = parse_conclude({"outcome": "finding", "summary": "x", "what_changed": "5xx errors up 12x",
                               "since": past.isoformat(), "failing": "502s on /ui",
                               "likely_cause": "a bad deploy"})
    ck("accepted", got is not None, str(err))
    eq("the four fields", (got["what_changed"], got["since"], got["failing"], got["likely_cause"]),
       ("5xx errors up 12x", past, "502s on /ui", "a bad deploy"))
    got, _ = parse_conclude({"outcome": "finding", "summary": "x",
                             "since": (now + timedelta(hours=2)).isoformat()})
    eq("a since in the future is dropped", got["since"], None)
    got, _ = parse_conclude({"outcome": "finding", "summary": "x", "since": "last tuesday-ish"})
    eq("a since that is no time is dropped", got["since"], None)
    got, _ = parse_conclude({"outcome": "finding", "summary": "x"})
    eq("none given, none kept", (got["what_changed"], got["since"], got["failing"],
                                 got["likely_cause"]), (None, None, None, None))
    got, _ = parse_conclude({"outcome": "finding", "summary": "x", "what_changed": "w" * 90,
                             "failing": "f" * 400})
    ck("long values are cut", len(got["what_changed"]) <= 60 and len(got["failing"]) <= 200,
       str((len(got["what_changed"]), len(got["failing"]))))

    print("== narration is not a headline ==")
    eq("a stats sentence is skipped",
       G.derive_headline("The stats give me what I need. Checkout returns 502s."),
       "Checkout returns 502s.")
    eq("a wider-read sentence is skipped",
       G.derive_headline("The wider read confirms the picture cleanly. The cart service is slow."),
       "The cart service is slow.")
    eq("a plain statement is kept",
       G.derive_headline("The checkout service returns 502s since 14:05."),
       "The checkout service returns 502s since 14:05.")
    eq("a sentence that names the failure is kept even when it opens like narration",
       G.derive_headline("The logs show errors on checkout. The cart service is slow."),
       "The logs show errors on checkout.")
    eq("so is one that gives a number or a status class",
       G.derive_headline("The data shows 5xx up. The cart service is slow."), "The data shows 5xx up.")

    print("== the parts of a numbered note ==")
    parts = G.derive_parts(NOTE)
    ck("what is failing", (parts["failing"] or "").startswith("The edge-proxy returns 502s"), str(parts))
    ck("the likely cause", (parts["likely_cause"] or "").startswith("The 14:02 deploy"), str(parts))
    ck("the next step", (parts["next_step"] or "").startswith("Roll service-a back"), str(parts))
    eq("the next step is the third part", G.derive_next_step(NOTE), parts["next_step"])
    eq("a note with no parts gives none", G.derive_parts("Next, we should look."),
       {"failing": None, "likely_cause": None, "next_step": None})
    eq("a labelled next step still wins",
       G.derive_next_step("Something broke.\n\nNext step: restart the pods."), "Restart the pods.")
    rca = ("## Root cause analysis\n\nThe edge-proxy returns 502s since 14:02.\n\n"
           "2) Most likely cause: the 14:02 deploy of service-a.\n\n3) Suggested next action: roll it back.")
    eq("a Root cause analysis heading is not the likely cause",
       G.derive_parts(rca)["likely_cause"], "The 14:02 deploy of service-a.")
    eq("and it is not the headline either", G.derive_headline("## Root cause analysis\n\nThe proxy is down."),
       "Root cause analysis")
    eq("a heading that only starts with a label word gives no part",
       G.derive_parts("## Root cause analysis\n\nIt broke."), {"failing": None, "likely_cause": None, "next_step": None})
    eq("a part label with a dash counts", G.derive_parts("Root cause - the 14:02 deploy.")["likely_cause"],
       "The 14:02 deploy.")
    eq("Next.js is not a next step", G.derive_parts("Next.js builds are slow.")["next_step"], None)
    eq("a heading on its own line still counts", G.derive_parts("## Next steps\n\nRoll it back.")["next_step"],
       "Roll it back.")

    print("== the title ==")
    now = datetime(2026, 10, 30, 16, 30, tzinfo=timezone.utc)

    def t(since, what="5xx errors up 12x", key="checkout", headline=None):
        return G.title(G.title_parts_of(key, what, headline, since, now, {}), now)

    eq("today", t(datetime(2026, 10, 30, 14, 5, tzinfo=timezone.utc)), "checkout: 5xx errors up 12x since 14:05")
    eq("yesterday", t(datetime(2026, 10, 29, 23, 40, tzinfo=timezone.utc)),
       "checkout: 5xx errors up 12x since yesterday, 23:40")
    eq("older", t(datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)),
       "checkout: 5xx errors up 12x since 27 Oct, 09:00")
    ck("no em dash", "—" not in t(None, what="latency — up 3x"), t(None, what="latency — up 3x"))
    eq("with no since it is the time the cell saw it", t(None), "checkout: 5xx errors up 12x since 16:30")
    eq("from the headline when the agent gave no words",
       t(datetime(2026, 10, 30, 14, 5, tzinfo=timezone.utc), what=None,
         headline="The checkout service returns 502s since 14:05."),
       "checkout: the checkout service returns 502s since 14:05")
    eq("an acronym keeps its capitals",
       t(datetime(2026, 10, 30, 14, 5, tzinfo=timezone.utc), what="API errors up 4x"),
       "checkout: API errors up 4x since 14:05")

    print("== the outcome ==")
    sched = {"watch-prod"}
    watcher = {"trigger": "watch-prod", "woken_by": "manual", "outcome": "finding", "verdict": "investigate"}
    handed = {"trigger": "watch-prod", "woken_by": "handoff", "outcome": "finding"}
    eq("a watcher with nothing to say is quiet",
       G.outcome_of({**watcher, "outcome": "no_op"}, G.is_watcher_run(watcher, sched)), "quiet")
    eq("a watcher that hands on raised it", G.outcome_of(watcher, G.is_watcher_run(watcher, sched)), "raised")
    for v in ("ok", "benign", "no_action"):
        eq(f"a watcher finding with the verdict {v} is not raised",
           G.outcome_of({**watcher, "verdict": v}, G.is_watcher_run(watcher, sched)), "investigated")
    eq("an investigator that says resolved",
       G.outcome_of({**handed, "verdict": "resolved"}, G.is_watcher_run(handed, sched)), "investigated")
    eq("an investigator that writes an rca",
       G.outcome_of({**handed, "verdict": "rca"}, G.is_watcher_run(handed, sched)), "note_written")
    eq("a note with no verdict is a note",
       G.outcome_of({**handed, "verdict": None}, G.is_watcher_run(handed, sched)), "note_written")
    eq("every outcome has words", sorted(G.OUTCOME_WORDS),
       ["investigated", "note_written", "quiet", "raised"])
    ck("no em dash in the words", all("—" not in w for w in G.OUTCOME_WORDS.values()))
    eq("a note whose own next step says there is nothing to do is no action",
       G.kind_of("note_written", "No action required. Monitor the proxy."), "no_action")
    eq("a note with a numbered next step is an action", G.kind_of("note_written", G.derive_next_step(NOTE)), "action")


# ── the cell ─────────────────────────────────────────────────────────────────
async def cell():
    from tares.daemon import make_app
    threading.Thread(target=HTTPServer(("127.0.0.1", SLACK_PORT), Slack).serve_forever,
                     daemon=True).start()
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        store, runtime, runner = app.state.store, app.state.runtime, app.state.agents
        runner.attach_loop()
        store.set_setting(edition_mod.EDITION_SETTING, "sre")
        now = datetime.now(timezone.utc)
        made = build(store, now, watcher=True, investigator=True)
        runtime.reload_catalog()
        uid = made["project"]
        since = (now - timedelta(minutes=25)).replace(microsecond=0)

        def watch(key, **args):
            SCRIPT[("watcher", key)] = args
            rid = runner.run_now("watcher", "watch-prod", key, "", project=uid)
            return rid

        def investigate(key, parent, **args):
            SCRIPT[("investigator", key)] = args
            return runner.run_now("investigator", "watch-prod", key, "", woken_by="handoff",
                                  parent_run_id=parent, project=uid)

        print("== a watcher and an investigator, run for real ==")
        watch("checkout-web", outcome="no_op", summary="Nothing unusual in the last ten minutes.")
        w2 = watch("edge-proxy", outcome="finding", verdict="investigate",
                   summary="502s on the edge-proxy rose to 12x.", what_changed="5xx errors up 12x",
                   since=since.isoformat(), failing="502s on /ui", likely_cause="a bad deploy")
        w3 = watch("service-a", outcome="finding", verdict="investigate", summary="Jobs are failing.")
        w4 = watch("docs-site", outcome="finding", verdict="degraded",
                   summary="Builds on docs-site take twice as long.", what_changed="builds twice as slow")
        w5 = watch("cart-api", outcome="finding", verdict="investigate", summary="Cart latency is up.")
        for agent, n in (("watcher", 5),):
            await wait_for(lambda: settled(store, agent, n))
        investigate("edge-proxy", w2, outcome="finding", verdict="resolved",
                    summary="The spike was a load test. Nothing to do.",
                    headline="Load test from a staging client")
        investigate("service-a", w3, outcome="finding", verdict="rca", summary=NOTE)
        investigate("cart-api", w5, outcome="finding", verdict=None,
                    summary="Cart latency is up because the cache was cold after a restart.")
        await wait_for(lambda: settled(store, "investigator", 3))

        runs = {(r["agent"], r["key"]): r for r in store.list_agent_runs(None, limit=100)}
        ck("every run finished", len(runs) == 8 and all(r["status"] == "ok" for r in runs.values()),
           str([(k, r["status"]) for k, r in runs.items()]))
        w = runs[("watcher", "edge-proxy")]
        eq("the fields are stored on the run", (w["what_changed"], w["failing"], w["likely_cause"]),
           ("5xx errors up 12x", "502s on /ui", "a bad deploy"))
        ck("so is the time it started", w["since_at"] is not None
           and abs((w["since_at"].replace(tzinfo=timezone.utc) if w["since_at"].tzinfo is None
                    else w["since_at"]) - since) < timedelta(seconds=2), str(w["since_at"]))

        res = (await cx.get(f"/api/projects/{uid}/results")).json()
        by = {x["entity"]: x for x in res["results"]}
        eq("one result per key", sorted(by), ["cart-api", "checkout-web", "docs-site", "edge-proxy", "service-a"])
        eq("a watcher with nothing to say is quiet", by["checkout-web"]["outcome"], "quiet")
        eq("a watcher that did not hand on is raised", by["docs-site"]["outcome"], "raised")
        eq("an investigator that resolved looked into it", by["edge-proxy"]["outcome"], "investigated")
        eq("an rca is a note", by["service-a"]["outcome"], "note_written")
        eq("a finding with no verdict is a note", by["cart-api"]["outcome"], "note_written")
        eq("the words", [by[k]["outcome_words"] for k in ("checkout-web", "docs-site", "edge-proxy", "service-a")],
           ["Quiet", "Raised", "Looked into, nothing to do", "Note written"])
        eq("an rca is an action", by["service-a"]["kind"], "action")
        eq("a resolved one is not", by["edge-proxy"]["kind"], "no_action")
        eq("a quiet one is not", by["checkout-web"]["kind"], "no_action")
        eq("a note with a numbered next step is not 'no action'", by["service-a"]["next_step"].startswith("Roll service-a back"), True)

        top = by["service-a"]
        ck("the title starts with the service", top["title"].startswith("service-a: "), top["title"])
        ck("the title is not the agent's narration", "I now have" not in top["title"], top["title"])
        eq("the parts come from the numbered note", (top["failing"].startswith("The edge-proxy returns 502s"),
                                                      top["likely_cause"].startswith("The 14:02 deploy")), (True, True))
        # the investigator on edge-proxy gave nothing; the title still names the service
        ck("a title always names the service", all(x["title"].startswith(x["entity"] + ": ") for x in res["results"]),
           str([x["title"] for x in res["results"]]))
        ck("the title parts carry the service", by["docs-site"]["title_parts"]["service"] == "docs-site",
           str(by["docs-site"]["title_parts"]))
        eq("the agent's words are the title", by["docs-site"]["title"].split(" since ")[0],
           "docs-site: builds twice as slow")
        ck("no em dash anywhere in the results", "—" not in json.dumps(res))

        print("== show=worth leaves out the quiet ones ==")
        worth = (await cx.get(f"/api/projects/{uid}/results?show=worth")).json()
        eq("no quiet result", sorted(x["entity"] for x in worth["results"]),
           ["cart-api", "docs-site", "edge-proxy", "service-a"])
        eq("the roll-up counts what it left out", worth["quiet"]["count"], 1)
        ck("and says since when", bool(worth["quiet"]["since"]), str(worth["quiet"]))
        eq("no show has them, and counts none left out", (await cx.get(f"/api/projects/{uid}/results")).json()["quiet"]["count"], 0)
        det = (await cx.get(f"/api/projects/{uid}/results/{by['service-a']['id']}")).json()
        ck("the detail has them too", det["title"] == top["title"] and det["outcome"] == "note_written"
           and det["failing"] == top["failing"] and det["next_step"] == top["next_step"], str(det)[:300])

        print("== GET /api/findings, across projects ==")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "Docs watch", "objects": [],
                                                 "goal": "Keep the docs site fast"})
        assert r.status_code == 201, r.text
        other = r.json()["id"]
        r = await cx.post(f"/api/projects/{other}/findings", json={
            "entity": "docs-site", "finding": "The docs build failed twice on a missing image.",
            "agent": "outside-bot", "what_changed": "builds failing", "failing": "two failed builds",
            "likely_cause": "a missing image", "since": since.isoformat()})
        ck("an outside finding with the new fields is recorded", r.status_code == 201, r.text)
        ext_id = r.json()["run_id"]
        r = await cx.post(f"/api/projects/{other}/findings", json={
            "entity": "x", "finding": "y", "what_changed": "w" * 61, "agent": "outside-bot"})
        eq("a what_changed over 60 characters is refused", r.status_code, 400)
        r = await cx.post(f"/api/projects/{other}/findings", json={
            "entity": "x", "finding": "y", "since": "not a time", "agent": "outside-bot"})
        eq("a since that is no time is refused", r.status_code, 400)

        allf = (await cx.get("/api/findings")).json()
        eq("the shape", sorted(allf), ["findings", "next_before", "quiet"])
        got = {x["entity"] for x in allf["findings"]}
        eq("both projects, the quiet one left out", got,
           {"cart-api", "docs-site", "edge-proxy", "service-a"})
        eq("two docs-site findings, one from each project", sum(1 for x in allf["findings"] if x["entity"] == "docs-site"), 2)
        eq("newest first", [x["at"] for x in allf["findings"]], sorted((x["at"] for x in allf["findings"]), reverse=True))
        ext = next(x for x in allf["findings"] if x["id"] == ext_id)
        eq("an outside finding is a note, titled by its words", (ext["outcome"], ext["title"].split(" since ")[0],
                                                              ext["failing"], ext["likely_cause"]),
           ("note_written", "docs-site: builds failing", "two failed builds", "a missing image"))
        eq("the quiet roll-up", allf["quiet"]["count"], 1)
        ck("every finding has the new keys", all(
            {"title", "title_parts", "failing", "likely_cause", "next_step", "outcome", "outcome_words",
             "kind"} <= set(x) for x in allf["findings"]), str(allf["findings"][0].keys()))
        svc = (await cx.get("/api/findings?service=docs-site")).json()
        eq("service= keeps one service", {x["entity"] for x in svc["findings"]}, {"docs-site"})
        eq("service= keeps both projects' findings", len(svc["findings"]), 2)
        every = (await cx.get("/api/findings?show=all")).json()
        eq("show=all brings the quiet one back", "checkout-web" in {x["entity"] for x in every["findings"]}, True)
        eq("and counts none left out", every["quiet"]["count"], 0)
        page = (await cx.get("/api/findings?show=all&limit=3")).json()
        ck("a page and a cursor", len(page["findings"]) == 3 and page["next_before"], str(page)[:200])
        nxt = (await cx.get("/api/findings", params={"show": "all", "limit": 50, "before": page["next_before"]})).json()
        eq("the next page has the rest, once", len(page["findings"]) + len(nxt["findings"]), len(every["findings"]))
        eq("a bad show is refused", (await cx.get("/api/findings?show=some")).status_code, 400)
        eq("a bad cursor is refused", (await cx.get("/api/findings?before=yesterday")).status_code, 400)
        no_dash = "—" not in json.dumps(allf)
        ck("no em dash in the feed", no_dash)

        print("== handled, practice and shared threads ==")
        sid = by["service-a"]["id"]
        eq("service-a asks for action before it is handled", by["service-a"]["kind"], "action")
        r = await cx.post(f"/api/projects/{uid}/results/{sid}/handled", json={"handled": True})
        eq("marking it handled", r.status_code, 200)
        eq("a handled result is no action, in the detail", (await cx.get(f"/api/projects/{uid}/results/{sid}")).json()["kind"], "no_action")
        eq("and in the feed", next(x["kind"] for x in (await cx.get("/api/findings?show=all")).json()["findings"] if x["id"] == sid), "no_action")
        r = await cx.post(f"/api/projects/{uid}/results/{sid}/handled", json={"handled": False})
        eq("clearing the mark brings the action back", (await cx.get(f"/api/projects/{uid}/results/{sid}")).json()["kind"], "action")

        store.start_agent_run("run_practice1", "investigator", "watch-prod", "", "practice-svc", "h",
                              woken_by="handoff", project=other, practice=True)
        store.finish_agent_run("run_practice1", "ok", finding="The practice-svc service returns 500s.",
                               outcome="finding", verdict="rca")
        store.start_agent_run("run_shared1", "investigator", "watch-prod", "", "shared-svc", "h",
                              woken_by="handoff", project=uid)
        store.finish_agent_run("run_shared1", "ok", finding="The shared-svc service returns 500s.",
                               outcome="finding", verdict="rca")
        store.add_run_projects("run_shared1", [other])
        feed = (await cx.get("/api/findings?show=all&limit=100")).json()["findings"]
        eq("a practice result is not in the feed", [x for x in feed if x["entity"] == "practice-svc"], [])
        eq("a thread two projects share is listed once", [x["id"] for x in feed if x["entity"] == "shared-svc"], ["run_shared1"])

        print("== the Slack header ==")
        agent = {"name": "investigator", "prompt": "p", "slack_channel": "C123", "owned_by": uid}
        title_args = dict(what_changed="5xx errors up 12x", since=since)

        async def post(key="edge-proxy", **parts):
            SLACK.clear()
            await runner._record(agent, "watch-prod", key, NOTE, verdict="rca", run_id="run_slack1",
                                 model="claude-sonnet-5", headline=None, next_step=None, parts=parts)
            return SLACK[0] if SLACK else {}

        msg = await post(**title_args)
        head = msg["blocks"][0]["text"]["text"] if msg else ""
        eq("on an AI SRE cell the header is the title", head,
           G.title(G.title_parts_of("edge-proxy", "5xx errors up 12x", None, since, datetime.now(timezone.utc), {}),
                   datetime.now(timezone.utc)))
        ck("it names the service and what changed", head.startswith("edge-proxy: 5xx errors up 12x since "), head)
        fields = json.dumps(msg["blocks"][1])
        ck("a Service field comes first", "*Service*\\nedge-proxy" in fields
           and fields.index("*Service*") < fields.index("*Agent*"), fields)
        msg = await post(what_changed="x" * 60, since=since)
        ck("a header never passes Slack's limit", len(msg["blocks"][0]["text"]["text"]) <= 150,
           str(len(msg["blocks"][0]["text"]["text"])))
        msg = await post()
        ck("with no words the header is still a title", msg["blocks"][0]["text"]["text"].startswith("edge-proxy: "),
           msg["blocks"][0]["text"]["text"])

        store.set_setting(edition_mod.EDITION_SETTING, "platform")
        msg = await post(**title_args)
        eq("on a platform cell the header is the key, as before", msg["blocks"][0]["text"]["text"], "edge-proxy")
        ck("and there is no Service field", "Service" not in json.dumps(msg["blocks"][1]), json.dumps(msg["blocks"][1]))
        store.set_setting(edition_mod.EDITION_SETTING, "sre")

        print("== twenty older notes that open on narration ==")
        narration = ["Now I have the full picture.", "Let me put this together.",
                     "The stats give me what I need.", "The wider read confirms the picture cleanly.",
                     "I have enough information to write this up.", "Good, that settles it."]
        shapes = [
            "{n}\n\n## 1. What is failing\n\n{f}\n\n## 2. Most likely cause\n\n{c}\n\n## 3. Suggested next action\n\n{a}",
            "{n}\n\n1) What is failing\n{f}\n\n2) Most likely cause\n{c}\n\n3) Suggested next action\n{a}",
            "{n}\n\n**1. What is failing**\n\n{f}\n\n**2. Most likely cause**\n\n{c}\n\n**3. Suggested next action**\n\n{a}",
            "{n}\n\nWhat is failing: {f}\n\nRoot cause: {c}\n\nNext step: {a}",
        ]
        for i in range(20):
            svc = f"svc-{i:02d}"
            note = shapes[i % len(shapes)].format(
                n=narration[i % len(narration)], f=f"The {svc} service returns 503s on /orders since 09:{i:02d}.",
                c=f"A config change at 09:{i:02d} lowered the {svc} pool size.",
                a=f"Restore the previous {svc} config and watch the 503 rate.")
            rid = f"run_old_{i:02d}"
            store.start_agent_run(rid, "investigator", "watch-prod", "", svc, "h", woken_by="handoff", project=uid)
            store.finish_agent_run(rid, "ok", finding=note, outcome="finding", verdict="rca")
        old = (await cx.get("/api/findings?show=all&limit=100")).json()["findings"]
        mine = [x for x in old if x["entity"].startswith("svc-")]
        eq("twenty results", len(mine), 20)
        bad = [x["title"] for x in mine if any(n.rstrip(".") in x["title"] for n in narration)
               or not x["title"].startswith(x["entity"] + ": ") or len(x["title"]) <= len(x["entity"]) + 4]
        eq("none of the titles is narration", bad, [])
        eq("each title says what is failing, not the section label",
           [x["title"] for x in mine if "503s" not in x["title"] or "what is failing" in x["title"].lower()], [])
        eq("all are notes", {x["outcome"] for x in mine}, {"note_written"})
        eq("all have a next step and a kind of action", {(bool(x["next_step"]), x["kind"]) for x in mine},
           {(True, "action")})
        eq("all have the failing part and the cause", all(x["failing"] and x["likely_cause"] for x in mine), True)


if __name__ == "__main__":
    pure()
    asyncio.run(cell())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
