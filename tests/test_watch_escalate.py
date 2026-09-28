"""Watch then escalate, end to end through the daemon (TR-320, TR-321, TR-322).

A schedule trigger ticks on a view, a triage agent (the `triage` preset) is handed the window's
summary and concludes `investigate` on one entity, a findings view filtered to that verdict wakes a
root-cause agent (the `rca-from-triage` preset), and its note lands on the same entity. On the next
tick, triage concludes `no_op` and nothing escalates.

The model is a stub Anthropic endpoint that plays both agents from their prompts, so the test
checks the wiring, not a model's judgment. Takes a little over a minute: the second tick waits out
the one-minute minimum interval.
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

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


TMP = tempfile.mkdtemp()
DB, SEED = os.path.join(TMP, "t.duckdb"), os.path.join(TMP, "seed.yaml")
PORT, STUB_PORT = "8836", "8837"
CALLS = []            # every request the stub got
TRIAGE_TICKS = []     # the first user message of each triage run


def _tool_results(body):
    return sum(1 for m in body["messages"] if m["role"] == "user" and isinstance(m["content"], list))


class Stub(BaseHTTPRequestHandler):
    """Plays the two agents. Triage: one `stats` call, then `conclude` (investigate on the first
    tick, no_op after). Root cause: one `read`, then `conclude` on the same entity."""

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        CALLS.append(body)
        system = body.get("system") or ""
        step = _tool_results(body)
        if "You watch a system on a schedule" in system:
            if step == 0:
                TRIAGE_TICKS.append(body["messages"][0]["content"])
                content = [{"type": "tool_use", "id": "t1", "name": "stats",
                            "input": {"view": "watch", "by": "code", "where": {"service": "ui"},
                                      "window": "1m"}}]
            elif len(TRIAGE_TICKS) == 1:
                content = [{"type": "tool_use", "id": "t2", "name": "conclude", "input": {
                    "outcome": "finding", "verdict": "investigate", "key": "ui", "label": "service",
                    "summary": "ui: 404s new at 60 in the last window"}}]
            else:
                content = [{"type": "tool_use", "id": "t2", "name": "conclude", "input": {
                    "outcome": "no_op", "summary": "traffic as in the window before"}}]
        else:
            if step == 0:
                content = [{"type": "tool_use", "id": "r1", "name": "read",
                            "input": {"selector": {"service": "ui"}, "window": "1h"}}]
            else:
                content = [{"type": "tool_use", "id": "r2", "name": "conclude", "input": {
                    "outcome": "finding", "verdict": "rca", "key": "ui", "label": "service",
                    "summary": "ui serves 404 on every route since the last deploy"}}]
        out = json.dumps({"content": content, "model": body.get("model"), "stop_reason": "tool_use",
                          "usage": {"input_tokens": 100, "output_tokens": 20}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


async def until(fn, tries=120, every=0.5):
    for _ in range(tries):
        if await fn():
            return True
        await asyncio.sleep(every)
    return False


def guard_checks():
    """The findings loop guard lets a handoff through and nothing else."""
    from tares.config import CatalogError, validate_agent_dict
    trig = {"escalate": {"name": "escalate", "view": "f"}}

    def ok(agent, filters):
        try:
            validate_agent_dict({"name": agent, "trigger": "escalate", "prompt": "p"}, {"escalate"},
                                trig, {"f": {"name": "f", "sources": ["findings"], "filters": filters}})
            return True
        except CatalogError:
            return False
    print("== the findings loop guard ==")
    check("a view over all findings is still refused", not ok("rca", []))
    check("a view over the agent's own findings is refused",
          not ok("rca", [{"field": "agent", "op": "eq", "value": "rca"}]))
    check("a view over another agent's findings is a handoff, allowed",
          ok("rca", [{"field": "agent", "op": "eq", "value": "watcher"},
                     {"field": "verdict", "op": "eq", "value": "investigate"}]))


async def main():
    guard_checks()
    with open(SEED, "w") as fh:
        fh.write(
            "sources:\n"
            "  - name: logs\n    connector: webhook\n    poll: 5s\n"
            "    config:\n      labels:\n"
            "        - {name: service, field: service, primary: true}\n"
            "        - {name: code, field: code}\n"
            "  - name: findings\n    connector: finding\n    poll: 5s\n    config: {}\n"
            "views:\n"
            "  - {name: watch, key_field: service, sources: [logs]}\n"
            "  - name: triage-investigate\n    key_field: service\n    sources: [findings]\n"
            "    filters:\n"
            "      - {field: agent, op: eq, value: watcher}\n"
            "      - {field: verdict, op: eq, value: investigate}\n"
            "triggers:\n"
            "  - name: escalate\n    view: triage-investigate\n    cooldown: 30m\n"
            "    condition: {aggregate: count, predicate: '> 0', window: 5m}\n")
    Stub.timeout = 5
    threading.Thread(target=HTTPServer(("127.0.0.1", int(STUB_PORT)), Stub).serve_forever,
                     daemon=True).start()
    env = {**os.environ, "TARES_DB": DB, "TARES_CATALOG": SEED, "TARES_PORT": PORT,
           "TARES_MCP_PORT": str(int(PORT) + 10), "TARES_OTLP_GRPC_PORT": "off",
           "ANTHROPIC_API_KEY": "sk-test", "TARES_ANTHROPIC_BASE": f"http://127.0.0.1:{STUB_PORT}",
           "TARES_TRIGGER_DEBOUNCE_SECONDS": "0", "TARES_SCHEDULE_CLOCK_SECONDS": "1"}
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

            # the stream: ui starts serving 404s, api is steady
            events = ([{"service": "ui", "code": "404", "msg": "GET /x 404"}] * 60
                      + [{"service": "api", "code": "200", "msg": "GET /y 200"}] * 10)
            r = await cx.post(f"{B}/ingest/logs", json=events)
            check("events ingested", r.status_code == 202, r.text[:200])

            for name, trigger, preset in (("watcher", "watch-tick", "triage"),
                                          ("rca", "escalate", "rca-from-triage")):
                if name == "watcher":
                    r = await cx.post(f"{B}/api/triggers", json={
                        "name": "watch-tick", "view": "watch",
                        "condition": {"every": "1m", "summary_by": ["service", "code"]}})
                    check("schedule trigger created over the API", r.status_code == 201, r.text[:200])
                r = await cx.post(f"{B}/api/agents/builtin", json={
                    "name": name, "trigger": trigger, "prompt": ba.PRESETS[preset]["prompt"]})
                check(f"agent {name} created with the {preset} preset", r.status_code == 201, r.text[:200])
                await cx.post(f"{B}/api/agents/builtin/{name}/enable")

            async def runs(agent):
                return (await cx.get(f"{B}/api/agents/builtin/{agent}/runs")).json()

            async def done(agent, n):
                rs = await runs(agent)
                return len(rs) >= n and all(r["status"] != "running" for r in rs)

            print("== tick 1: triage flags ui, the root-cause agent follows ==")
            check("triage ran on the first tick", await until(lambda: done("watcher", 1)))
            w = (await runs("watcher"))[0]
            check("triage concluded investigate", w["status"] == "ok" and w.get("outcome") == "finding"
                  and w.get("verdict") == "investigate", json.dumps(w)[:300])
            first = TRIAGE_TICKS[0] if TRIAGE_TICKS else ""
            check("triage was handed the window summary, not raw lines",
                  'schedule "watch-tick" ticked' in first and "service | now | before | change" in first
                  and "ui | 60 | 0 | new" in first, first[:600])
            stats_out = [m for c in CALLS for m in c["messages"]
                         if m["role"] == "user" and isinstance(m["content"], list)
                         and any("code | now | before" in str(b.get("content")) for b in m["content"])]
            check("its stats call came back with counts", bool(stats_out))

            check("the root-cause agent was woken", await until(lambda: done("rca", 1)))
            rc = (await runs("rca"))[0]
            check("woken for the entity triage named", rc["key"] == "ui" and rc["trigger"] == "escalate",
                  json.dumps(rc)[:300])
            check("its note is a finding with verdict rca", rc.get("outcome") == "finding"
                  and rc.get("verdict") == "rca", json.dumps(rc)[:300])
            woke = next((c for c in CALLS if "root-cause analysis" in (c.get("system") or "")), None)
            check("it was handed the triage finding", woke is not None
                  and "404s new at 60" in woke["messages"][0]["content"])

            r = (await cx.post(f"{B}/read", json={"selector": {"service": "ui"}, "window": "1h"})).json()
            finding_rows = [x for x in r["rows"] if x.get("source") == "findings"]
            check("both notes are on ui's timeline", len(finding_rows) == 2
                  and "404s new at 60" in r["payload"] and "since the last deploy" in r["payload"],
                  r["payload"][-600:])
            both = await cx.get(f"{B}/api/sources")
            rows = [(s["name"], s["health"]["events_total"]) for s in both.json() if s["name"] == "findings"]
            check("two findings recorded (triage, root cause)", rows and rows[0][1] == 2, str(rows))

            print("== tick 2: a normal window stays quiet ==")
            check("triage ran on the next tick (after the 1m interval)",
                  await until(lambda: done("watcher", 2), tries=90, every=1.0))
            w2 = (await runs("watcher"))[0]
            check("it concluded no_op", w2["status"] == "ok" and w2.get("outcome") == "no_op",
                  json.dumps(w2)[:300])
            await asyncio.sleep(3)
            check("nothing escalated", len(await runs("rca")) == 1)
            fl = [(s["name"], s["health"]["events_total"]) for s in (await cx.get(f"{B}/api/sources")).json()
                  if s["name"] == "findings"]
            check("no finding for the quiet tick", fl and fl[0][1] == 2, str(fl))

            print("== the off switch ==")
            await cx.post(f"{B}/api/triggers/watch-tick/pause")
            n = len(await runs("watcher"))
            await asyncio.sleep(3)
            check("a paused schedule wakes no one", len(await runs("watcher")) == n)
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
