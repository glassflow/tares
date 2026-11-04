"""Slack buttons that act (TR-762).

On an AI SRE cell the message a note opens an incident with carries buttons: "I'm on it", "Resolve"
or "Reopen" by the incident's state, and "Worth raising" / "Not worth it" on what a watcher raised.
Covers the message builders (the state line, the buttons and their values per state, the platform
message unchanged byte for byte), a signed click on each button (state changes with via "slack",
Slack is answered 200 at once, the clicked message is replaced, a thread reply says who did what),
an unsigned or forged click (401), an unknown incident (an ephemeral reply), a button from another
workspace (ignored), a link button (nothing), a malformed payload (200), and the hook that follows
a console or sweep change into Slack (chat.update and a thread reply; not for a click, not on a
platform cell, and a failing hook never breaks the change).

The model loop is a stand-in that concludes from a script per (agent, key) with the real
parse_conclude; the run path, the incident hook, the Slack post, the routes and the click endpoint
are the real ones, against a stub Slack API.

Run: .venv/bin/python tests/test_slack_incident_buttons.py
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import threading
import types
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

SLACK_PORT = 8869
TOKEN = "root-token"
SECRET = "signing-secret-for-tests"
TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
os.environ["TARES_SLACK_API_BASE"] = f"http://127.0.0.1:{SLACK_PORT}"
os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-test"
os.environ["TARES_SLACK_SIGNING_SECRET"] = SECRET
os.environ["TARES_SLACK_CONNECT_URL"] = "https://console.example.com/slack/install?workspace=team-a"
os.environ["TARES_PUBLIC_URL"] = "https://cell.example.com"
os.environ["TARES_AUTH_TOKEN"] = TOKEN   # read when the daemon is imported

import httpx  # noqa: E402

from fixtures.sre_cell import build  # noqa: E402
from tares import incidents, slack, slack_actions, slack_verify, words  # noqa: E402
from tares.builtin_agents import AgentRunner, parse_conclude  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


SCRIPT: dict = {}
CALLS: list = []   # (path, body) the stub Slack API was sent


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    args = SCRIPT.get((agent["name"], key), {"outcome": "no_op", "summary": ""})
    got, err = parse_conclude(args)
    assert got is not None, err
    concluded.update(got)
    return got["summary"], 1, 1, [], False, ""


AgentRunner._loop = fake_loop


class Slack(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        CALLS.append((self.path, body))
        data = json.dumps({"ok": True, "channel": body.get("channel"),
                           "ts": f"1790000000.{len(CALLS):06d}"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def calls(path):
    return [b for p, b in CALLS if p == path]


async def wait_for(fn, tries=200):
    for _ in range(tries):
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.03)
    return fn()


def H(tok=TOKEN):
    return {"Authorization": f"Bearer {tok}"}


def action_ids(msg):
    return [e["action_id"] for b in msg["blocks"] if b["type"] == "actions" for e in b["elements"]]


def buttons(msg):
    return {e["action_id"]: e for b in msg["blocks"] if b["type"] == "actions"
            for e in b["elements"]}


def contexts(msg):
    return [b["elements"][0]["text"] for b in msg.get("blocks", []) if b["type"] == "context"]


def digest(msg):
    return hashlib.sha256(json.dumps(msg, sort_keys=True).encode()).hexdigest()


# ── the builders, without a cell ─────────────────────────────────────────────
def builders():
    note = "Summary: The 14:02 deploy of service-a left the proxy on old pods."
    plain = dict(verdict="rca", model="claude-sonnet-5", run_id="run_1",
                 when="2026-09-29T07:30:51+00:00", full_note=True,
                 title="edge-proxy: 502s since 14:02", service="edge-proxy")
    print("== a platform cell's message is the message it always was ==")
    msg = slack.build_finding_message("investigator", "watch-prod", "edge-proxy", note, **plain)
    # sha256 of the message the code on the base branch builds for this call
    eq("byte for byte, with everything given",
       digest(msg), "b4edfceee8c4269f183644c55a0dd091e55444bdf02f9dc923a98e1651102d0c")
    msg2 = slack.build_finding_message("investigator", "watch-prod", "edge-proxy", note,
                                       verdict="rca", run_id="run_1")
    eq("byte for byte, with the short call",
       digest(msg2), "cab119fc55fa607c8c6ddabee6454ce862de45ce200acc48ee9853c4c261d865")
    eq("explicit Nones change nothing",
       slack.build_finding_message("investigator", "watch-prod", "edge-proxy", note, **plain,
                                   incident=None, workspace=None, worth_run=None), msg)
    eq("only link buttons", sorted(action_ids(msg)), ["tares_link_0", "tares_link_1"])

    print("== an AI SRE message: buttons and state line by state ==")
    base = {"id": "inc_1", "service": "edge-proxy", "who": None, "resolved": None}
    since = "2026-09-29T14:12:00+00:00"
    states = {
        "open": ({**base, "state": "open"}, ["tares_incident_on_it", "tares_incident_resolve"],
                 "Open, nobody on it yet"),
        "on_it": ({**base, "state": "on_it", "who": {"name": "Alex", "since": since}},
                  ["tares_incident_resolve", "tares_incident_on_it"], "Alex is on it since "),
        "resolved": ({**base, "state": "resolved", "resolved": {
            "by_name": "Alex", "at": "2026-09-29T15:03:00+00:00", "how": "person"}},
                     ["tares_incident_reopen"], "Resolved by Alex at "),
        "itself": ({**base, "state": "resolved", "resolved": {
            "by_name": None, "at": "2026-09-30T15:03:00+00:00", "how": "itself"}},
                   ["tares_incident_reopen"], "Resolved itself"),
    }
    for name, (inc, want_ids, line) in states.items():
        m = slack.build_finding_message("investigator", "watch-prod", "edge-proxy", note, **plain,
                                        incident=inc, workspace="team-a", worth_run="run_1")
        ids = action_ids(m)
        eq(f"{name}: the incident buttons", [i for i in ids if i.startswith("tares_incident_")],
           want_ids)
        eq(f"{name}: the worth pair and the links stay",
           [i for i in ids if not i.startswith("tares_incident_")],
           ["tares_worth_yes", "tares_worth_no", "tares_link_0", "tares_link_1"])
        ck(f"{name}: the state line", any(c.startswith(line) for c in contexts(m)), str(contexts(m)))
        for b in buttons(m).values():
            if b["action_id"].startswith("tares_incident_"):
                eq(f"{name}: {b['action_id']} value", json.loads(b["value"]),
                   {"i": "inc_1", "w": "team-a"})
            if b["action_id"].startswith("tares_worth_"):
                eq(f"{name}: {b['action_id']} value", json.loads(b["value"]),
                   {"r": "run_1", "w": "team-a"})
            if b["action_id"].startswith("tares_link_"):
                ck(f"{name}: {b['action_id']} stays a link", b.get("url") and "value" not in b)
        new_text = [b["text"]["text"] for b in m["blocks"] if b["type"] == "actions"
                    for b in b["elements"] if b["action_id"].startswith(("tares_incident_", "tares_worth_"))]
        new_text += [slack.incident_state_line(inc)]
        ck(f"{name}: the new words are clean", all(words.offenders(t) == [] for t in new_text),
           [words.offenders(t) for t in new_text])
    inc, _ids, _line = states["on_it"]
    ck("the clock shows in each reader's zone",
       "<!date^" in slack.incident_state_line(inc) and "{time}" in slack.incident_state_line(inc))
    eq("a name cannot make a mention",
       slack.incident_state_line({**inc, "who": {"name": "<!channel>", "since": since}}).split(" is on")[0],
       "&lt;!channel&gt;")
    only_worth = slack.build_finding_message("a", "t", "k", note, worth_run="run_1")
    eq("worth alone adds only its pair",
       [i for i in action_ids(only_worth) if not i.startswith("tares_link")],
       ["tares_worth_yes", "tares_worth_no"])
    eq("the handlers cover the prefixes", sorted(slack_actions.HANDLERS),
       ["tares_incident_", "tares_link_", "tares_worth_"])


# ── a click as Slack sends it ────────────────────────────────────────────────
def click_body(action_id, value, user=("U111", "Alex"), ts="1790000000.000001", **over):
    payload = {"type": "block_actions", "user": {"id": user[0], "name": user[1]},
               "channel": {"id": "C1"}, "container": {"message_ts": ts, "channel_id": "C1"},
               "message": {"ts": ts}, "response_url": f"http://127.0.0.1:{SLACK_PORT}/resp",
               "actions": [{"action_id": action_id,
                            "value": value if isinstance(value, str) else json.dumps(value)}]}
    payload.update(over)
    return urlencode({"payload": json.dumps(payload)}).encode()


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

        eq("edition set to AI SRE",
           (await cx.put("/api/settings/edition", json={"edition": "sre"})).status_code, 200)
        made = build(store, base, watcher=True, investigator=True)
        uid = made["project"]
        store.set_handoffs(uid, "watcher", [{"verdict": "investigate", "agent": "investigator",
                                             "cooldown": "30m"}])
        store.upsert_catalog_agent(
            "investigator", "watch-prod", "Find the cause of what the watcher flagged.",
            budget_usd=25.0, concludes=True, slack_channel="C1")
        runtime.reload_catalog()

        async def finish(rid):
            return await wait_for(lambda: (lambda x: x if x and x["status"] != "running" else None)(
                store.get_agent_run(rid)))

        async def investigate(run_key, parent, **args):
            SCRIPT[("investigator", run_key)] = args
            rid = runner.run_now("investigator", "watch-prod", run_key, "", woken_by="handoff",
                                 parent_run_id=parent, project=uid)
            row = await finish(rid)
            assert row and row["status"] == "ok", row
            return rid

        async def click(action_id, value, signed=True, **kw):
            raw = click_body(action_id, value, **kw)
            ts = str(int(time.time()))
            headers = {"content-type": "application/x-www-form-urlencoded"}
            if signed:
                headers.update({"x-slack-request-timestamp": ts,
                                "x-slack-signature": slack_verify.sign(ts, raw, SECRET)})
            return await cx.post("/api/slack/events", content=raw, headers=headers)

        SCRIPT[("watcher", "edge-proxy")] = {"outcome": "finding", "verdict": "investigate",
                                             "summary": "502s on the edge-proxy rose to 12x."}
        w1 = runner.run_now("watcher", "watch-prod", "edge-proxy", "", project=uid)
        await finish(w1)
        await asyncio.sleep(0.3)

        print("== the message a note opens an incident with ==")
        n1 = await investigate("edge-proxy", w1, outcome="finding", verdict="rca",
                               summary="The 14:02 deploy of service-a left the proxy on old pods.")
        first = [b for b in calls("/chat.postMessage") if not b.get("thread_ts")][-1]
        iid = store.incident_for_run(n1)["id"]
        first_ts = store.get_incident(iid)["slack"]["ts"]
        eq("incident buttons, the worth pair, then the links", action_ids(first),
           ["tares_incident_on_it", "tares_incident_resolve", "tares_worth_yes",
            "tares_worth_no", "tares_link_0", "tares_link_1"])
        eq("the incident button carries the incident and this cell's workspace",
           json.loads(buttons(first)["tares_incident_on_it"]["value"]), {"i": iid, "w": "team-a"})
        eq("the worth button carries the run",
           json.loads(buttons(first)["tares_worth_yes"]["value"]), {"r": n1, "w": "team-a"})
        ck("the state line says nobody is on it", "Open, nobody on it yet" in contexts(first),
           str(contexts(first)))

        print("== a note joined under it carries no incident buttons ==")
        clock[0] = base + timedelta(minutes=10)
        n2 = await investigate("edge-proxy", w1, outcome="finding", verdict="rca",
                               summary="The proxy still points at pods the deploy removed.")
        reply = [b for b in calls("/chat.postMessage") if b.get("thread_ts") == first_ts][-1]
        eq("only the worth pair and links",
           action_ids(reply), ["tares_worth_yes", "tares_worth_no", "tares_link_0", "tares_link_1"])

        print("== the same note on a platform cell is the message it always was ==")
        eq("edition set to platform",
           (await cx.put("/api/settings/edition", json={"edition": "platform"})).status_code, 200)
        clock[0] = base + timedelta(hours=2)
        n_before = len(calls("/chat.postMessage"))
        await investigate("service-a", w1, outcome="finding", verdict="rca",
                          summary="Posting jobs fails with a timeout.")
        msg = calls("/chat.postMessage")[n_before]
        eq("only link buttons", sorted(action_ids(msg)), ["tares_link_0", "tares_link_1"])
        ck("no state line", all("nobody on it" not in c for c in contexts(msg)), str(contexts(msg)))
        eq("edition back to AI SRE",
           (await cx.put("/api/settings/edition", json={"edition": "sre"})).status_code, 200)

        print("== a click is refused when it is not signed ==")
        value = {"i": iid, "w": "team-a"}
        n = len(CALLS)
        eq("unsigned", (await click("tares_incident_on_it", value, signed=False)).status_code, 401)
        raw = click_body("tares_incident_on_it", value)
        r = await cx.post("/api/slack/events", content=raw, headers={
            "content-type": "application/x-www-form-urlencoded",
            "x-slack-request-timestamp": str(int(time.time())), "x-slack-signature": "v0=bad"})
        eq("forged", r.status_code, 401)
        await asyncio.sleep(0.3)
        eq("so nothing changed", (store.get_incident(iid)["state"], len(CALLS)), ("open", n))

        print("== I'm on it ==")
        t0 = len(CALLS)
        r = await click("tares_incident_on_it", value)
        eq("Slack is answered 200", r.status_code, 200)
        await wait_for(lambda: calls("/resp") and
                       [b for b in calls("/chat.postMessage") if b.get("thread_ts") == first_ts
                        and "is on it" in b["text"]])
        inc = store.get_incident(iid)
        eq("on it, by the person who clicked", (inc["state"], inc["owner"], inc["owner_name"]),
           ("on_it", "slack:U111", "Alex"))
        ck("the log says it came from Slack",
           ("on_it", "slack") in [(e["action"], e["via"]) for e in store.incident_log(iid)])
        resp = calls("/resp")[-1]
        eq("the clicked message is replaced", resp["replace_original"], True)
        eq("with Resolve and Take over", [i for i in action_ids(resp)
                                          if i.startswith("tares_incident_")],
           ["tares_incident_resolve", "tares_incident_on_it"])
        ck("and the state line", any(c.startswith("Alex is on it since") for c in contexts(resp)),
           str(contexts(resp)))
        thread = [b for b in calls("/chat.postMessage")[t0:] if b.get("thread_ts") == first_ts]
        eq("the thread says who", [b["text"] for b in thread], ["Alex is on it"])
        await asyncio.sleep(0.3)
        eq("and the message was not also updated by the hook", calls("/chat.update"), [])
        n = len(CALLS)
        await click("tares_incident_on_it", value)
        await asyncio.sleep(0.4)
        eq("the same click again says nothing new in the thread",
           [b for b in calls("/chat.postMessage")[t0:] if "is on it" in b.get("text", "")].__len__(), 1)

        print("== Resolve ==")
        r = await click("tares_incident_resolve", value)
        eq("answered 200", r.status_code, 200)
        await wait_for(lambda: store.get_incident(iid)["state"] == "resolved")
        inc = store.get_incident(iid)
        eq("resolved by them", (inc["state"], inc["resolved_by"], inc["resolved_by_name"]),
           ("resolved", "slack:U111", "Alex"))
        ck("via Slack", ("resolved", "slack") in [(e["action"], e["via"])
                                                  for e in store.incident_log(iid)])
        eq("its notes count as handled by them", store.get_agent_run(n1)["handled_by"], "Alex")
        await wait_for(lambda: [b for b in calls("/chat.postMessage")
                                if b.get("text") == "Alex resolved this"])
        resp = calls("/resp")[-1]
        eq("the message offers Reopen", [i for i in action_ids(resp)
                                         if i.startswith("tares_incident_")],
           ["tares_incident_reopen"])
        ck("and says who resolved it", any(c.startswith("Resolved by Alex at") for c in contexts(resp)),
           str(contexts(resp)))

        print("== Reopen ==")
        await click("tares_incident_reopen", value, user=("U222", "Kim"))
        await wait_for(lambda: store.get_incident(iid)["state"] == "open")
        ck("open again, via Slack", store.get_incident(iid)["state"] == "open" and
           ("reopened", "slack") in [(e["action"], e["via"]) for e in store.incident_log(iid)])
        await wait_for(lambda: [b for b in calls("/chat.postMessage") if b.get("text") == "Kim reopened this"])
        eq("the thread says who", bool([b for b in calls("/chat.postMessage")
                                         if b.get("text") == "Kim reopened this"]), True)
        eq("no click was copied by the hook", calls("/chat.update"), [])

        print("== a console change follows into Slack ==")
        clock[0] += timedelta(seconds=5)    # a later log row, so "the last reopen" is not a tie
        u0, p0 = len(calls("/chat.update")), len(calls("/chat.postMessage"))
        r = await cx.post(f"/api/incidents/{iid}/on_it")
        eq("the console change answers", r.status_code, 200)
        who = r.json()["who"]["name"]
        await wait_for(lambda: len(calls("/chat.update")) > u0)
        upd = calls("/chat.update")[-1]
        eq("chat.update names the message", (upd["channel"], upd["ts"]), ("C1", first_ts))
        ck("with the new state", any(c.startswith(f"{who} is on it since") for c in contexts(upd)),
           str(contexts(upd)))
        await wait_for(lambda: any(c["text"] == f"{who} is on it in Tares" for c in calls("/chat.postMessage")[p0:]))
        post = [c for c in calls("/chat.postMessage")[p0:] if c["text"] == f"{who} is on it in Tares"][-1]
        eq("and the thread says it", (post["thread_ts"], post["text"]),
           (first_ts, f"{who} is on it in Tares"))
        r = await cx.post(f"/api/incidents/{iid}/resolve")
        await wait_for(lambda: len(calls("/chat.update")) > u0 + 1)
        await wait_for(lambda: calls("/chat.postMessage")[-1]["text"] == f"{who} resolved this in Tares")
        eq("a resolve reads the same way", calls("/chat.postMessage")[-1]["text"],
           f"{who} resolved this in Tares")
        await cx.post(f"/api/incidents/{iid}/reopen")
        await wait_for(lambda: calls("/chat.postMessage")[-1]["text"] == f"{who} reopened this in Tares")
        eq("so does a reopen", calls("/chat.postMessage")[-1]["text"], f"{who} reopened this in Tares")

        print("== two quick changes: the message shows where the incident is now ==")
        stale = store.get_incident(iid)
        eq("a copy taken while it was open", stale["state"], "open")
        out = store.set_incident_state(iid, "resolved", "console:pat", "Pat", "console",
                                       resolution="person", expect="open")
        u0, p0 = len(calls("/chat.update")), len(calls("/chat.postMessage"))
        await slack_actions._tell_slack(store, stale)
        eq("the message is updated once", len(calls("/chat.update")), u0 + 1)
        ck("and shows the resolve, not the copy", any(c.startswith("Resolved by Pat at")
                                                       for c in contexts(calls("/chat.update")[-1])),
           str(contexts(calls("/chat.update")[-1])))
        eq("the thread says the resolve", calls("/chat.postMessage")[-1]["text"], "Pat resolved this in Tares")
        incidents.reopen(store, iid, None, None, via="slack")   # back to open, no copy

        print("== the sweep follows into Slack ==")
        u0, p0 = len(calls("/chat.update")), len(calls("/chat.postMessage"))
        inc = store.get_incident(iid)
        out = store.set_incident_state(iid, "resolved", None, None, "sweep", resolution="itself",
                                       expect="open")
        incidents._changed(store, out, "sweep")
        await wait_for(lambda: any(c["text"].startswith("Resolved itself") for c in calls("/chat.postMessage")[p0:]))
        eq("the thread says it resolved itself", [c["text"] for c in calls("/chat.postMessage")[p0:]
                                                  if c["text"].startswith("Resolved itself")][-1],
           "Resolved itself, nothing new for 24 hours")
        ck("and the message shows it", any(c == "Resolved itself" for c in contexts(calls("/chat.update")[-1])),
           str(contexts(calls("/chat.update")[-1])))
        incidents.reopen(store, iid, None, None, via="slack")   # a click's reopen: no copy

        print("== a failing hook does not break the change ==")
        def broken(store_, inc_, via_):
            raise RuntimeError("hook down")
        incidents.ON_CHANGE.insert(0, broken)
        orig = slack_actions._tell_slack

        async def explode(store_, inc_):
            raise RuntimeError("slack down")
        slack_actions._tell_slack = explode
        try:
            r = await cx.post(f"/api/incidents/{iid}/on_it")
            eq("still answers 200", r.status_code, 200)
            eq("and the state changed", store.get_incident(iid)["state"], "on_it")
            await asyncio.sleep(0.3)
        finally:
            slack_actions._tell_slack = orig
            incidents.ON_CHANGE.remove(broken)

        print("== a platform cell's incident changes tell Slack nothing ==")
        await cx.put("/api/settings/edition", json={"edition": "platform"})
        n = len(CALLS)
        await cx.post(f"/api/incidents/{iid}/resolve")
        await asyncio.sleep(0.4)
        eq("no calls", len(CALLS), n)
        eq("a click does nothing there", [(await click("tares_incident_reopen", value)).status_code,
                                           store.get_incident(iid)["state"]], [200, "resolved"])
        await cx.put("/api/settings/edition", json={"edition": "sre"})
        await asyncio.sleep(0.3)
        eq("and sends nothing", len(CALLS), n)

        print("== things a click should not act on ==")
        state = store.get_incident(iid)["state"]
        n = len(CALLS)
        eq("a button of another workspace is answered 200",
           (await click("tares_incident_reopen", {"i": iid, "w": "team-b"})).status_code, 200)
        await asyncio.sleep(0.4)
        eq("and ignored", (store.get_incident(iid)["state"], len(CALLS)), (state, n))
        eq("a link button is answered 200",
           (await click("tares_link_0", "")).status_code, 200)
        await asyncio.sleep(0.3)
        eq("and does nothing", (store.get_incident(iid)["state"], len(CALLS)), (state, n))
        r0 = len(calls("/resp"))
        n = len(CALLS) + 1   # the one ephemeral reply below
        eq("an incident that is gone is answered 200",
           (await click("tares_incident_on_it", {"i": "inc_gone", "w": "team-a"})).status_code, 200)
        await wait_for(lambda: len(calls("/resp")) > r0)
        eq("with a reply only the clicker sees", calls("/resp")[-1],
           {"response_type": "ephemeral", "replace_original": False,
            "text": "This incident is no longer in Tares."})
        eq("a value that is not JSON is ignored",
           (await click("tares_incident_on_it", "not json")).status_code, 200)
        eq("and so is a JSON value that is not an object, with no reply either",
           [slack_actions.dispatch(store, {"type": "block_actions", "user": {"id": "U1", "name": "Alex"},
                                           "actions": [{"action_id": a, "value": v}]}, "team-a")
            for a in ("tares_incident_on_it", "tares_worth_yes") for v in ('[1, 2]', '"x"', "7")],
           [{}] * 6)
        eq("a payload that is not JSON is ignored", (await cx.post(
            "/api/slack/events", content=b"payload=%7Bnot", headers={
                "content-type": "application/x-www-form-urlencoded",
                "x-slack-request-timestamp": (ts := str(int(time.time()))),
                "x-slack-signature": slack_verify.sign(ts, b"payload=%7Bnot", SECRET)})).status_code, 200)
        eq("a click with no user is ignored",
           (await click("tares_incident_on_it", value, user=("", ""))).status_code, 200)
        await asyncio.sleep(0.4)
        eq("none of them changed anything", (store.get_incident(iid)["state"], len(CALLS)), (state, n))

        print("== Was this worth raising? ==")
        cx.headers.update(H())
        wv = {"r": n1, "w": "team-a"}
        r = await click("tares_worth_yes", wv)
        eq("answered 200", r.status_code, 200)
        await wait_for(lambda: store.feedback_for([n1])[n1])
        row = store.feedback_for([n1])[n1][0]
        eq("recorded for the person, via Slack",
           (row["person"], row["person_name"], row["via"], row["worth"], row["watcher"]),
           ("slack:U111", "Alex", "slack", True, "watcher"))
        await wait_for(lambda: [b for b in calls("/chat.postMessage")
                                if b.get("text") == "Alex says this was worth raising"])
        eq("the thread acknowledges it", bool([b for b in calls("/chat.postMessage")
                                               if b.get("text") == "Alex says this was worth raising"]), True)
        await wait_for(lambda: [c for c in calls("/resp") if "Alex said worth raising" in contexts(c)])
        resp = [c for c in calls("/resp") if "Alex said worth raising" in contexts(c)][-1]
        eq("the message is replaced with the answer and keeps both buttons",
           (resp["replace_original"], [i for i in action_ids(resp) if i.startswith("tares_worth_")]),
           (True, ["tares_worth_yes", "tares_worth_no"]))
        t1 = len([b for b in calls("/chat.postMessage") if "worth raising" in b.get("text", "")])
        await click("tares_worth_yes", wv)
        await asyncio.sleep(0.4)
        eq("the same answer again says nothing new in the thread",
           len([b for b in calls("/chat.postMessage") if "worth raising" in b.get("text", "")]), t1)
        await click("tares_worth_no", wv)
        await wait_for(lambda: not store.feedback_for([n1])[n1][0]["worth"])
        rows = store.feedback_for([n1])[n1]
        eq("the same person answering again keeps one row", [(x["person"], x["worth"]) for x in rows],
           [("slack:U111", False)])
        await click("tares_worth_yes", wv, user=("U222", "Kim"))
        await wait_for(lambda: len(store.feedback_for([n1])[n1]) == 2)
        eq("a second person adds one", sorted((x["person_name"], x["worth"], x["via"])
                                              for x in store.feedback_for([n1])[n1]),
           [("Alex", False, "slack"), ("Kim", True, "slack")])
        await wait_for(lambda: [c for c in calls("/resp")
                                if "1 said worth raising, 1 said not worth raising" in contexts(c)])
        r0 = len(calls("/resp"))
        await click("tares_worth_maybe", wv, user=("U333", "Lee"))
        await asyncio.sleep(0.3)
        eq("a worth button that is neither yes nor no stores nothing",
           len(store.feedback_for([n1])[n1]), 2)
        await click("tares_worth_yes", {"r": "run_gone", "w": "team-a"})
        await wait_for(lambda: len(calls("/resp")) > r0)
        eq("a result that is gone gets an ephemeral reply", calls("/resp")[-1]["text"],
           "This result is no longer in Tares.")
        quiet = store.start_agent_run("run_quiet", "investigator", "watch-prod", "", "x", "h",
                                      woken_by="handoff", project=uid)
        store.finish_agent_run("run_quiet", "ok", finding="", outcome="no_op")
        r0 = len(calls("/resp"))
        await click("tares_worth_no", {"r": "run_quiet", "w": "team-a"})
        await wait_for(lambda: len(calls("/resp")) > r0)
        eq("a result nothing raised gets one too", calls("/resp")[-1]["text"],
           "Only what the watcher raised can be answered")
        eq("and no answer is kept", store.feedback_for(["run_quiet"])["run_quiet"], [])

        print("== a worth click keeps the message it was made on ==")
        first_resp = [c for c in calls("/resp") if "Alex said worth raising" in contexts(c)][-1]
        ck("the incident's first note keeps the incident buttons",
           [i for i in action_ids(first_resp) if i.startswith("tares_incident_")] != [],
           str(action_ids(first_resp)))
        ck("and the first note's text", "14:02 deploy" in json.dumps(first_resp))
        sam = ("U555", "Sam")
        await click("tares_worth_yes", {"r": n2, "w": "team-a"}, user=sam)
        await wait_for(lambda: [c for c in calls("/resp") if "Sam said worth raising" in contexts(c)])
        joined = [c for c in calls("/resp") if "Sam said worth raising" in contexts(c)][-1]
        eq("a joined note is replaced with itself", joined["replace_original"], True)
        ck("with its own text, not the incident's first note",
           "still points at pods" in json.dumps(joined) and "14:02 deploy" not in json.dumps(joined))
        eq("with no incident buttons and no incident state line",
           ([i for i in action_ids(joined) if i.startswith("tares_incident_")],
            [c for c in contexts(joined) if "nobody on it" in c or " is on it" in c]), ([], []))
        eq("and the worth pair for that note",
           [json.loads(b["value"]) for k, b in buttons(joined).items() if k.startswith("tares_worth_")],
           [{"r": n2, "w": "team-a"}, {"r": n2, "w": "team-a"}])
        ck("a watcher finding that opened no incident", store.incident_for_run(w1) is None)
        await click("tares_worth_no", {"r": w1, "w": "team-a"}, user=sam)
        await wait_for(lambda: [c for c in calls("/resp") if "Sam said not worth raising" in contexts(c)])
        alone = [c for c in calls("/resp") if "Sam said not worth raising" in contexts(c)][-1]
        eq("is replaced with itself", alone["replace_original"], True)
        ck("with the watcher's own text", "502s on the edge-proxy rose" in json.dumps(alone)
           and alone["text"].startswith("watcher on edge-proxy:"), alone.get("text", ""))
        eq("and no incident on it", [i for i in action_ids(alone) if i.startswith("tares_incident_")], [])
        eq("the answer is kept", [(x["person"], x["worth"]) for x in store.feedback_for([w1])[w1]],
           [("slack:U555", False)])

        print("== a name is escaped and capped; a note that is gone ==")
        t2 = len(calls("/chat.postMessage"))
        await click("tares_worth_no", wv, user=("U444", "<!here> & " + "x" * 100))
        # an earlier click's thread post can still be on its way: wait for this person's own
        await wait_for(lambda: any("&lt;!here&gt;" in b["text"] for b in calls("/chat.postMessage")[t2:]))
        said = [b["text"] for b in calls("/chat.postMessage")[t2:] if "&lt;!here&gt;" in b["text"]][0]
        ck("the name cannot mention anyone", "<!here>" not in said and "&lt;!here&gt; &amp; " in said, said)
        raw_name = "<!here> & " + "x" * 100
        eq("and is kept cut at 80 characters",
           [x["person_name"] for x in store.feedback_for([n1])[n1] if x["person"] == "slack:U444"],
           [raw_name[:80]])
        real = slack.build_incident_message
        slack.build_incident_message = lambda *a, **k: None
        try:
            r0 = len(calls("/resp"))
            await click("tares_incident_resolve", value)
            await wait_for(lambda: len(calls("/resp")) > r0)
        finally:
            slack.build_incident_message = real
        eq("a click on a message whose note is gone says so", calls("/resp")[-1]["text"],
           slack_actions.GONE_NOTE)

        print("== which message the incident keeps ==")
        class Fake:
            def __init__(self, first):
                self.first, self.set_on = first, []
            def incident_notes(self, i):
                return [{"run_id": self.first}]
            def set_note_slack(self, run_id, channel, ts):
                pass
            def set_incident_slack(self, i, channel, ts):
                self.set_on.append(ts)
        keep = type(runner)._keep_slack
        fake = Fake("run_first")
        keep(types.SimpleNamespace(store=fake), {"id": "i"}, "run_later", {"channel": "C1", "ts": "2"})
        keep(types.SimpleNamespace(store=fake), {"id": "i"}, "run_first", {"channel": "C1", "ts": "1"})
        eq("a later note's post is not the incident's message", fake.set_on, ["1"])

        print("== the rebuilt message and the words ==")
        store.set_service(store.get_incident(iid)["service"], "tester", display="Edge Proxy")
        inc = store.get_incident(iid)
        msg = slack.build_incident_message(store, inc, "team-a")
        eq("with the service's display name", [f["text"] for b in msg["blocks"] if b["type"] == "section"
                                                for f in b.get("fields", []) if f["text"].startswith("*Service")]
           , ["*Service*\nEdge Proxy"])
        ck("it is the first note's message", msg is not None and
           msg["text"].startswith("investigator on edge-proxy:"), str(msg and msg["text"]))
        joined_at = "2020-03-04T05:06:07+00:00"
        real_notes = store.incident_notes
        store.incident_notes = lambda i: [{**n, "joined_at": joined_at} for n in real_notes(i)]
        try:
            dated = slack.build_incident_message(store, store.get_incident(iid), "team-a")
        finally:
            store.incident_notes = real_notes
        ck("it is dated from when the note joined the incident",
           slack._slack_date(joined_at) in json.dumps(dated), json.dumps(dated)[:300])
        every = [b.get("text", "") for b in calls("/chat.postMessage")[-8:]]
        ck("the thread replies are clean", all(words.offenders(t) == [] for t in every),
           [words.offenders(t) for t in every])
        await cx.aclose()


if __name__ == "__main__":
    builders()
    asyncio.run(cell())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
