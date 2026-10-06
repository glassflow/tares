"""A decision model as the watcher (TR-324, TR-381, TR-382).

First the pieces: the endpoint settings (validation, the token never returned), the questions a
window turns into, and how answers become an outcome. Then end to end through the daemon: a
decision endpoint is saved and tested over the API, a watcher on a schedule trigger asks it about
the window, and
- in shadow mode it records the probability and the entity but concludes no_op, waking no one;
- with shadow off, at or above the threshold it concludes investigate on that entity and hands it
  to the root-cause agent;
- below the threshold it concludes no_op.

The decision endpoint and the root-cause agent's model are one stub server, so the test checks the
wiring, not a model's judgment.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tares.builtin_agents as ba
from tares import decision as dm

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


class FakeStore:
    def __init__(self):
        self.settings = {}

    def get_setting(self, k):
        return self.settings.get(k)

    def set_setting(self, k, v):
        if v is None:
            self.settings.pop(k, None)
        else:
            self.settings[k] = v


def raises(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return ""
    except ValueError as e:
        return str(e) or "raised"


def unit_checks():
    print("== endpoints ==")
    st = FakeStore()
    acct = "0123456789abcdef0123456789abcdef"
    check("a Cloudflare endpoint needs a 32-hex Account ID",
          "Account ID" in raises(dm.save_endpoint, st, "", "cloudflare", "CF", "tok", "nope"))
    check("and a token", "token" in raises(dm.save_endpoint, st, "", "cloudflare", "CF", "", acct))
    eid = dm.save_endpoint(st, "", "cloudflare", "My Cloudflare", "secret-token", acct)
    check("saved under the slug of its name", eid == "my-cloudflare")
    listing = dm.list_endpoints(st)
    check("the listing never carries the token",
          "secret-token" not in json.dumps(listing) and listing["endpoints"][0]["key_stored"])
    dm.save_endpoint(st, eid, "cloudflare", "", "", acct)
    check("a blank token keeps the stored one", dm.entry(st, eid)["key"] == "secret-token")
    check("an endpoint keeps its kind",
          "keeps its kind" in raises(dm.save_endpoint, st, eid, "typesafe", "", "k"))
    url, headers, extra = dm._request(dm.entry(st, eid), "clef-flash")
    check("Workers AI URL carries the account and the model",
          url.endswith(f"/accounts/{acct}/ai/run/@cf/cloudflare/clef-flash")
          and headers["Authorization"] == "Bearer secret-token", url)
    check("a custom endpoint needs an http(s) URL",
          "URL" in raises(dm.save_endpoint, st, "", "custom", "Kev", "", "", "ftp://x"))

    print("== agent settings ==")
    check("no decision settings is a chat-model agent", dm.normalize("a", None) == {})
    check("an endpoint is required", "endpoint" in raises(dm.normalize, "a", {"threshold": 0.4}))
    check("the threshold is between 0 and 1",
          "between 0 and 1" in raises(dm.normalize, "a", {"endpoint": "x", "threshold": 1.5}))
    n = dm.normalize("a", {"endpoint": "x"})
    check("defaults: threshold 0.5, not shadow", n["threshold"] == 0.5 and n["shadow"] is False)

    print("== questions and outcome ==")
    rows = [{"value": "ui", "now": 60, "before": 0, "change": "new"},
            {"value": "/docs/a b", "now": 12, "before": 10, "change": "+2 (x1.2)"}]
    qs, ids = dm.questions("", "service", rows)
    check("problem is a noul with the default wording",
          qs["problem"]["type"] == "noul" and "SRE" in qs["problem"]["instructions"])
    check("entity options are ids, values in the descriptions, plus none",
          set(qs["entity"]["criteria"]) == {"o1", "o2", "none"}
          and "service=ui (now 60, before 0, change new)" == qs["entity"]["criteria"]["o1"])
    settings = {"threshold": 0.5, "shadow": False}
    ans = {"problem": {"probability": 0.91},
           "entity": {"choice": "o1", "confidence": 0.8, "probabilities": {"o1": 0.8, "o2": 0.15,
                                                                          "none": 0.05}}}
    c, s = dm.outcome(ans, settings, "service", ids, None)
    check("above the threshold: investigate on the chosen entity",
          c["outcome"] == "finding" and c["verdict"] == "investigate" and c["key"] == "ui"
          and c["label"] == "service" and "now 60, before 0" in c["summary"], json.dumps(c))
    check("the scores keep the probabilities",
          s["problem"] == 0.91 and s["entity"] == "ui" and s["escalate"] and s["entity_p"] == 0.8
          and s["options"][0] == {"value": "ui", "p": 0.8}, json.dumps(s))
    c, s = dm.outcome(ans, {**settings, "shadow": True}, "service", ids, None)
    check("shadow: no_op, but says what it would have done",
          c["outcome"] == "no_op" and c["summary"].startswith("shadow: would have escalated service=ui")
          and s["escalate"] and s["shadow"], json.dumps(c))
    c, s = dm.outcome({**ans, "problem": 0.2}, settings, "service", ids, None)
    check("below the threshold: no_op (a bare number reads too)",
          c["outcome"] == "no_op" and "below the threshold" in c["summary"] and not s["escalate"])
    c, s = dm.outcome({**ans, "entity": {"choice": "none", "probabilities": {"none": 0.7}}},
                      settings, "service", ids, None)
    check("above the threshold but no entity: no_op", c["outcome"] == "no_op" and not s["escalate"])
    c, s = dm.outcome({"problem": {"probabilities": {"true": 0.7, "false": 0.3}}}, settings,
                      "service", {}, "checkout")
    check("the Workers AI noul shape reads", dm.probability({"type": "noul", "noul": 0.9003}) == 0.9003)
    check("a trigger that fired on one entity: that entity",
          c["outcome"] == "finding" and c["key"] == "checkout")
    try:
        dm.outcome({"problem": {"label": "yes"}}, settings, "service", ids, None)
        check("an answer without a probability is an error", False)
    except dm.DecisionError:
        check("an answer without a probability is an error", True)


TMP = tempfile.mkdtemp()
DB, SEED = os.path.join(TMP, "t.duckdb"), os.path.join(TMP, "seed.yaml")
PORT, STUB_PORT = "8846", "8847"
DECISIONS = []        # every body the decision stub got
PROBLEM = [0.9]       # what the stub answers next for `problem`
CHAT = []


class Stub(BaseHTTPRequestHandler):
    """/decide plays the decision model; anything else plays the root-cause agent's model."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if self.path == "/decide":
            DECISIONS.append({"body": body, "auth": self.headers.get("authorization")})
            answers = {"problem": {"probability": PROBLEM[0]}}
            ent = body["questions"].get("entity")
            if ent:
                ui = next((k for k, v in ent["criteria"].items() if "service=ui " in v), "none")
                answers["entity"] = {"choice": ui, "confidence": 0.8,
                                     "probabilities": {ui: 0.8, "none": 0.2}}
            # wrapped the way Workers AI wraps a model's output
            out = {"result": {"model": body.get("model"), "answers": answers,
                              "usage": {"input_tokens": 1000}}, "success": True}
        else:
            CHAT.append(body)
            step = sum(1 for m in body["messages"]
                       if m["role"] == "user" and isinstance(m["content"], list))
            content = ([{"type": "tool_use", "id": "r1", "name": "read",
                         "input": {"selector": {"service": "ui"}, "window": "1h"}}] if step == 0 else
                       [{"type": "tool_use", "id": "r2", "name": "conclude", "input": {
                           "outcome": "finding", "verdict": "rca", "key": "ui", "label": "service",
                           "summary": "ui serves 404 on every route since the last deploy"}}])
            out = {"content": content, "model": body.get("model"), "stop_reason": "tool_use",
                   "usage": {"input_tokens": 100, "output_tokens": 20}}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


async def until(fn, tries=120, every=0.5):
    for _ in range(tries):
        if await fn():
            return True
        await asyncio.sleep(every)
    return False


async def e2e():
    with open(SEED, "w") as fh:
        fh.write(
            "sources:\n"
            "  - name: logs\n    connector: webhook\n    poll: 5s\n"
            "    config:\n      labels:\n"
            "        - {name: service, field: service, primary: true}\n"
            "        - {name: code, field: code}\n")
    threading.Thread(target=HTTPServer(("127.0.0.1", int(STUB_PORT)), Stub).serve_forever,
                     daemon=True).start()
    # no chat model key on purpose: the decision watcher must enable and run without one; the
    # root-cause agent gets its (stub) key from Settings later
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "OPENAI_", "TARES_PLATFORM_PROVIDER"))}
    env.update({"TARES_DB": DB, "TARES_CATALOG": SEED, "TARES_PORT": PORT,
                "TARES_MCP_PORT": str(int(PORT) + 10), "TARES_OTLP_GRPC_PORT": "off",
                "TARES_TRIGGER_DEBOUNCE_SECONDS": "0", "TARES_SCHEDULE_CLOCK_SECONDS": "1",
                "TARES_WEBHOOK_ALLOW_PRIVATE": "1"})
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{PORT}"
    try:
        async with httpx.AsyncClient(timeout=20) as cx:
            async def up():
                try:
                    return (await cx.get(f"{B}/health")).status_code == 200
                except httpx.HTTPError:
                    return False
            if not await until(up):
                check("daemon up", False); return

            print("== the endpoint, from Settings ==")
            r = await cx.put(f"{B}/api/settings/decision-endpoints/new", json={
                "kind": "custom", "name": "Stub", "url": f"http://127.0.0.1:{STUB_PORT}/decide",
                "key": "stub-key"})
            check("saved", r.status_code == 200 and r.json()["id"] == "stub", r.text[:300])
            check("the key is not returned", "stub-key" not in r.text)
            r = await cx.post(f"{B}/api/settings/decision-endpoints/stub/test", json={})
            check("the Test button says it works", r.json().get("ok") is True
                  and r.json().get("probability") == 0.9, r.text[:300])
            check("the key went out as a bearer token", DECISIONS
                  and DECISIONS[-1]["auth"] == "Bearer stub-key")

            events = ([{"service": "ui", "code": "404", "msg": "GET /x 404"}] * 60
                      + [{"service": "api", "code": "200", "msg": "GET /y 200"}] * 10)
            r = await cx.post(f"{B}/ingest/logs", json=events)
            check("events ingested", r.status_code == 202, r.text[:200])

            r = await cx.post(f"{B}/api/agents/builtin", json={
                "name": "rca", "trigger": "", "prompt": ba.PRESETS["rca-from-triage"]["prompt"],
                "concludes": True})
            check("the root-cause agent created", r.status_code == 201, r.text[:200])
            r = await cx.post(f"{B}/api/triggers", json={
                "name": "watch-tick", "sources": ["logs"], "key_field": "service",
                "condition": {"every": "1m", "summary_by": ["service", "code"]}})
            check("schedule trigger created", r.status_code == 201, r.text[:200])
            r = await cx.post(f"{B}/api/agents/builtin", json={
                "name": "watcher", "trigger": "watch-tick", "concludes": True,
                "prompt": ba.PRESETS["triage-decision"]["prompt"],
                "decision": {"endpoint": "nope"}})
            check("an unknown endpoint is refused", r.status_code == 404, r.text[:200])
            watcher = {"name": "watcher", "trigger": "watch-tick", "concludes": True,
                       "prompt": ba.PRESETS["triage-decision"]["prompt"],
                       "verdicts": ba.PRESETS["triage-decision"]["verdicts"],
                       "handoffs": [{"verdict": "investigate", "agent": "rca"}],
                       "decision": {"endpoint": "stub", "threshold": 0.5, "shadow": True}}
            r = await cx.post(f"{B}/api/agents/builtin", json=watcher)
            check("the decision watcher created, in shadow mode", r.status_code == 201, r.text[:200])
            agents = (await cx.get(f"{B}/api/agents/builtin")).json()
            row = next(a for a in agents["agents"] if a["name"] == "watcher")
            check("the agent lists its decision settings",
                  row["decision"] == {"endpoint": "stub", "model": "", "threshold": 0.5,
                                      "shadow": True}, json.dumps(row["decision"]))
            check("the agents listing offers the endpoints and the preset",
                  [e["id"] for e in agents["decision_endpoints"]] == ["stub"]
                  and any(p["id"] == "triage-decision" for p in agents["presets"]))
            r = await cx.delete(f"{B}/api/settings/decision-endpoints/stub")
            check("an endpoint in use cannot be removed", r.status_code == 409, r.text[:200])
            r = await cx.post(f"{B}/api/agents/builtin/watcher/enable")
            check("it enables with no chat model provider on the cell", r.status_code == 200,
                  r.text[:200])
            # the root-cause agent's model, for the live part
            r = await cx.put(f"{B}/api/settings/providers/anthropic", json={
                "kind": "anthropic", "key": "sk-test", "base_url": f"http://127.0.0.1:{STUB_PORT}"})
            check("a chat model provider added for the root-cause agent", r.status_code == 200,
                  r.text[:200])

            async def runs(agent):
                return (await cx.get(f"{B}/api/agents/builtin/{agent}/runs")).json()

            async def done(agent, n):
                rs = await runs(agent)
                return len(rs) >= n and all(r["status"] != "running" for r in rs)

            print("== shadow: scores, wakes no one ==")
            n0 = len(DECISIONS)
            check("the watcher ran on the first tick", await until(lambda: done("watcher", 1)))
            w = (await runs("watcher"))[0]
            check("it concluded no_op", w["status"] == "ok" and w.get("outcome") == "no_op",
                  json.dumps(w)[:400])
            check("and says it would have escalated ui",
                  (w.get("finding") or "").startswith("shadow: would have escalated service=ui"),
                  w.get("finding"))
            sc = w.get("scores") or {}
            check("the run carries the probability and the entity",
                  sc.get("problem") == 0.9 and sc.get("entity") == "ui" and sc.get("escalate")
                  and sc.get("shadow"), json.dumps(sc))
            check("tokens and cost recorded on the run",
                  w.get("input_tokens") == 1000 and w.get("rounds") == 1, json.dumps(w)[:400])
            body = DECISIONS[n0]["body"] if len(DECISIONS) > n0 else {}
            check("the state is the window summary",
                  "service | now | before | change" in str(body.get("state")), str(body)[:400])
            check("the prompt is what counts as a problem",
                  body.get("questions", {}).get("problem", {}).get("instructions")
                  == ba.PRESETS["triage-decision"]["prompt"])
            crit = body.get("questions", {}).get("entity", {}).get("criteria", {})
            check("entity options are the window's services, plus none",
                  any("service=ui " in v for v in crit.values())
                  and any("service=api " in v for v in crit.values()) and "none" in crit,
                  json.dumps(crit))
            await asyncio.sleep(2)
            check("nothing was handed off", len(await runs("rca")) == 0)

            print("== live: above the threshold wakes the root-cause agent ==")
            r = await cx.put(f"{B}/api/agents/builtin/watcher",
                             json={**watcher, "decision": {**watcher["decision"], "shadow": False}})
            check("shadow turned off", r.status_code == 200, r.text[:200])
            r = await cx.post(f"{B}/api/agents/builtin/watcher/runs/{w['id']}/rerun")
            check("rerun started", r.status_code == 201, r.text[:200])
            check("the rerun finished", await until(lambda: done("watcher", 2)))
            w2 = (await runs("watcher"))[0]
            check("it concluded investigate on ui", w2.get("outcome") == "finding"
                  and w2.get("verdict") == "investigate" and "service=ui" in (w2.get("finding") or ""),
                  json.dumps(w2)[:400])
            check("the root-cause agent was woken for ui", await until(lambda: done("rca", 1)))
            rc = (await runs("rca"))[0]
            check("woken on the entity the watcher named", rc["key"] == "ui"
                  and rc.get("verdict") == "rca", json.dumps(rc)[:300])

            print("== live: below the threshold stays quiet ==")
            PROBLEM[0] = 0.1
            await cx.post(f"{B}/api/agents/builtin/watcher/runs/{w['id']}/rerun")
            check("the next run finished", await until(lambda: done("watcher", 3)))
            w3 = (await runs("watcher"))[0]
            check("it concluded no_op below the threshold", w3.get("outcome") == "no_op"
                  and "below the threshold" in (w3.get("finding") or ""), json.dumps(w3)[:400])
            await asyncio.sleep(2)
            check("nothing more handed off", len(await runs("rca")) == 1)

            print("== the daily cap does not apply ==")
            await cx.put(f"{B}/api/settings/agents", json={"daily_cap": 1})
            await cx.post(f"{B}/api/agents/builtin/watcher/runs/{w['id']}/rerun")
            check("another run finished", await until(lambda: done("watcher", 4)))
            check("it was not capped", (await runs("watcher"))[0]["status"] == "ok",
                  json.dumps((await runs("watcher"))[0])[:300])

            print("== the export ==")
            r = await cx.get(f"{B}/api/agents/builtin/watcher/runs.csv")
            lines = r.text.strip().splitlines()
            check("one row per run under a header", r.status_code == 200 and len(lines) == 5
                  and lines[0].startswith("started_at,status,outcome"), r.text[:300])
            check("the shadow run reads as would-escalate, in shadow",
                  ",0.9,0.5,ui,0.8,yes,yes," in lines[-1], lines[-1])
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    unit_checks()
    asyncio.run(e2e())
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
