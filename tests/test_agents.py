"""Tares agents — a prompt attached to a trigger writes a finding back onto the entity's timeline.

End-to-end against a stub Anthropic endpoint (TARES_ANTHROPIC_BASE): create source/view/trigger/
agent, enable it (= subscribe to the trigger), ingest until the trigger fires, and assert the agent
runs as a unified subscriber — it appears in the roster and the firing's deliveries, the finding
lands in the `findings` source, and the boundaries hold (disabled agents don't run, the loop guard
rejects an agent woken by findings, the key is never returned).
"""
import asyncio, json, os, signal, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

SEED = "/tmp/agents_catalog.yaml"
with open(SEED, "w") as fh:
    fh.write(
        "sources:\n  - name: evt\n    connector: webhook\n    poll: 5s\n"
        "    config:\n      labels:\n        - name: service\n          field: service\n"
        "          primary: true\n"
        "views:\n  - name: svc\n    key_field: service\n    sources: [evt]\n"
        # short cooldown: the first batch below fires while the agent is still disabled, and the
        # second must be able to fire again straight after it's enabled
        "triggers:\n  - name: incident\n    view: svc\n    cooldown: 1s\n"
        "    condition:\n      aggregate: count\n      predicate: '>= 2'\n      window: 1m\n")

DB, PORT, STUB_PORT = "/tmp/agents.duckdb", "8806", "8807"
FINDING = "checkout is returning 500s since the 14:02 deploy; roll it back."

# ── stub Anthropic: one tool_use round, then the conclusion ──────────────────
_calls = []


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _calls.append(body)
        # First call: ask for a wider read (exercises the tool path). Second: conclude.
        if len(_calls) == 1:
            content = [{"type": "tool_use", "id": "tu_1", "name": "read",
                        "input": {"selector": {"service": "checkout"}, "window": "1h"}}]
        else:
            content = [{"type": "text", "text": FINDING}]
        out = json.dumps({"content": content, "model": body.get("model"),
                          "usage": {"input_tokens": 100, "output_tokens": 50}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


async def _wait(url, tries=80):
    for _ in range(tries):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


async def _until(fn, tries=60):
    """Poll an async predicate until true — agents run in the background, off the request path."""
    for _ in range(tries):
        if await fn():
            return True
        await asyncio.sleep(0.5)
    return False


async def main():
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)

    stub = HTTPServer(("127.0.0.1", int(STUB_PORT)), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    env = {**os.environ, "TARES_DB": DB, "TARES_CATALOG": SEED, "TARES_PORT": PORT,
           "TARES_OTLP_GRPC_PORT": "off", "ANTHROPIC_API_KEY": "sk-test",
           "TARES_ANTHROPIC_BASE": f"http://127.0.0.1:{STUB_PORT}",
           "TARES_TRIGGER_DEBOUNCE_SECONDS": "0"}
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{PORT}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient(timeout=20) as cx:
            # ── the key: env-provided, never returned ────────────────────────
            k = (await cx.get(f"{B}/api/settings/anthropic-key")).json()
            ck("key reported configured from env", k["configured"] and k["source"].startswith("env:"), str(k))
            ck("key value is never returned", "sk-test" not in json.dumps(k), str(k))

            # ── create ───────────────────────────────────────────────────────
            r = await cx.post(f"{B}/api/agents/builtin", json={
                "name": "first-look", "trigger": "incident", "prompt": "Take a first look."})
            ck("create agent -> 201", r.status_code == 201, r.text)
            ck("agent starts disabled", r.json().get("enabled") is False, r.text)

            r = await cx.post(f"{B}/api/agents/builtin", json={
                "name": "dup", "trigger": "nope", "prompt": "x"})
            ck("unknown trigger rejected (400)", r.status_code == 400, str(r.status_code))

            lst = (await cx.get(f"{B}/api/agents/builtin")).json()
            ck("presets offered to the form", len(lst.get("presets", [])) >= 3, str(lst.keys()))

            # ── a disabled agent does not run and is not a subscriber ────────
            for i in range(3):
                await cx.post(f"{B}/ingest/evt", json={"service": "checkout", "msg": f"500 #{i}"})
            await asyncio.sleep(3)
            runs = (await cx.get(f"{B}/api/agents/builtin/first-look/runs")).json()
            ck("disabled agent did not run", runs == [], str(runs))
            woken = (await cx.get(f"{B}/api/agents")).json()["agents"]
            ck("disabled agent not in the roster", not any(a["name"] == "first-look" for a in woken), str(woken))

            # ── enable = subscribe to the trigger ────────────────────────────
            r = await cx.post(f"{B}/api/agents/builtin/first-look/enable")
            ck("enable -> 200", r.status_code == 200, r.text)
            woken = (await cx.get(f"{B}/api/agents")).json()["agents"]
            me = next((a for a in woken if a["name"] == "first-look"), None)
            ck("enabled agent appears in the roster", me is not None, str(woken))
            ck("roster tags it kind=tares", me and me.get("kind") == "tares", str(me))
            ck("roster shows it woken by the trigger", me and "incident" in me.get("triggers", []), str(me))

            for i in range(3):
                await cx.post(f"{B}/ingest/evt", json={"service": "checkout", "msg": f"500 later #{i}"})

            async def _ran():
                rs = (await cx.get(f"{B}/api/agents/builtin/first-look/runs")).json()
                return bool(rs) and rs[0]["status"] != "running"
            ck("a run completed", await _until(_ran), "no completed run")

            runs = (await cx.get(f"{B}/api/agents/builtin/first-look/runs")).json()
            run = runs[0] if runs else {}
            ck("run status ok", run.get("status") == "ok", str(run))
            ck("run records the finding", run.get("finding") == FINDING, str(run.get("finding")))
            ck("run records the entity", run.get("key") == "checkout", str(run.get("key")))
            ck("run counted the tool call", (run.get("tool_calls") or 0) >= 1, str(run))
            ck("run is tied to a dispatch", bool(run.get("dispatch_id")), str(run.get("dispatch_id")))

            # ── model usage: tokens from the stub's usage block, cost priced at write time ──
            calls_made = run.get("rounds") or 0   # the stub concludes without the +1 call
            ck("run records the model", run.get("model") == "claude-sonnet-4-6", str(run.get("model")))
            ck("run sums input tokens over its calls",
               run.get("input_tokens") == 100 * calls_made, str(run))
            ck("run sums output tokens over its calls",
               run.get("output_tokens") == 50 * calls_made, str(run))
            want_cost = (100 * calls_made * 3.0 + 50 * calls_made * 15.0) / 1e6
            ck("run cost matches the sonnet rate", abs((run.get("cost_usd") or 0) - want_cost) < 1e-9,
               f"{run.get('cost_usd')} vs {want_cost}")

            lst = (await cx.get(f"{B}/api/agents/builtin")).json()
            me = next(a for a in lst["agents"] if a["name"] == "first-look")
            ck("agent list carries stats", me.get("stats", {}).get("runs") == 1, str(me.get("stats")))
            ck("stats sum the cost", abs((me["stats"].get("cost_usd") or 0) - want_cost) < 1e-9,
               str(me["stats"]))

            um = (await cx.get(f"{B}/api/usage/model")).json()
            ck("usage meter totals the run's tokens",
               um["total"]["input_tokens"] == 100 * calls_made
               and um["total"]["output_tokens"] == 50 * calls_made, str(um["total"]))
            ck("usage meter attributes it to the agent surface",
               um["by_surface"].get("agent", {}).get("calls") == calls_made, str(um["by_surface"]))
            ck("usage meter has a per-day tail", len(um.get("days") or []) == 1, str(um.get("days")))

            # ── the firing counts the agent as a delivered subscriber ────────
            async def _delivered():
                d = (await cx.get(f"{B}/api/activity/dispatches")).json()
                return d and d[0]["subscribers"] >= 1 and d[0]["delivered"] >= 1 and d[0].get("pending", 0) == 0
            ck("recent firing counts the agent as delivered", await _until(_delivered),
               str((await cx.get(f"{B}/api/activity/dispatches")).json()[:1]))
            disp = (await cx.get(f"{B}/api/activity/dispatches")).json()[0]
            detail = (await cx.get(f"{B}/api/activity/dispatches/{disp['dispatch_id']}")).json()
            names = [dv["agent"] for dv in detail.get("deliveries", [])]
            ck("firing detail lists the Tares agent as a delivery", "first-look" in names, str(names))

            # ── the roster counts are windowed, with the all-time total kept ──
            me = next(a for a in (await cx.get(f"{B}/api/agents")).json()["agents"]
                      if a["name"] == "first-look")
            ck("roster counts deliveries in the last 24h", me.get("delivered_ok_24h", 0) >= 1, str(me))
            ck("roster keeps the all-time total (every delivery here is fresh)",
               me.get("delivered_ok_total") == me.get("delivered_ok_24h"), str(me))
            ck("roster drops the unwindowed 'delivered_ok'", "delivered_ok" not in me, str(me))

            # ── the finding is an ordinary event on the entity's timeline ────
            rd = (await cx.post(f"{B}/read", json={"selector": {"service": "checkout"},
                                                   "window": "15m"})).json()
            ck("finding is on the entity's timeline", FINDING in rd["payload"], rd["payload"][:200])
            ck("findings source contributes to the read", "findings" in rd["sources"], str(rd["sources"]))

            srcs = {s["name"]: s for s in (await cx.get(f"{B}/api/sources")).json()}
            ck("findings source auto-provisioned", "findings" in srcs, str(list(srcs)))

            raw = (await cx.post(f"{B}/read", json={"selector": {"service": "checkout"},
                                                    "window": "15m", "include_payload": True})).json()
            fnd = [r for r in raw["rows"] if r["source"] == "findings"]
            ck("finding carries provenance", bool(fnd) and fnd[0]["raw"].get("prompt_hash")
               and fnd[0]["raw"].get("agent") == "first-look", str(fnd)[:200])

            # ── the loop guard: an agent may not be woken by findings ────────
            await cx.post(f"{B}/api/views", json={"name": "loop", "key_field": "service",
                                                  "sources": ["evt", "findings"]})
            await cx.post(f"{B}/api/triggers", json={
                "name": "loopy", "view": "loop", "cooldown": "5m",
                "condition": {"aggregate": "count", "predicate": ">= 1", "window": "1m"}})
            r = await cx.post(f"{B}/api/agents/builtin", json={
                "name": "recursive", "trigger": "loopy", "prompt": "x"})
            ck("agent on a findings-fed trigger rejected", r.status_code == 400, str(r.status_code))
            ck("rejection explains the loop", "fire itself" in r.text or "findings" in r.text, r.text)

            # ── export/import round-trip ─────────────────────────────────────
            y = (await cx.get(f"{B}/api/catalog/export")).text
            ck("agent is in the catalog export", "first-look" in y and "agents:" in y, y[-400:])
            ck("enabled state round-trips", "enabled: true" in y, y[-400:])

            # ── disable removes the subscription; delete removes the agent ───
            ck("disable -> 200", (await cx.post(f"{B}/api/agents/builtin/first-look/disable")).status_code == 200)
            woken = (await cx.get(f"{B}/api/agents")).json()["agents"]
            ck("disabled agent left the roster", not any(a["name"] == "first-look" for a in woken), str(woken))
            ck("delete -> 200", (await cx.delete(f"{B}/api/agents/builtin/first-look")).status_code == 200)
            left = (await cx.get(f"{B}/api/agents/builtin")).json()["agents"]
            ck("agent gone", not any(x["name"] == "first-look" for x in left), str(left))
    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except Exception: proc.kill()

    # ── a run orphaned by a killed daemon is reaped on the next boot ─────────
    # Runs live in the daemon process, so a `running` row that outlives it is an orphan: it would
    # otherwise stay running forever AND keep counting toward the daily cap.
    try:
        from tares.store import Store
        st = Store(DB)
        st.upsert_catalog_agent("ghost", "incident", "Take a first look.")
        st.start_agent_run("run_orphan", "ghost", "incident", "d_orphan", "checkout", "h")
        ck("orphan counts toward the cap while it is running", st.agent_runs_today("ghost") == 1)
        st.con.close()

        proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if not await _wait(f"{B}/health"):
                ck("daemon back up", False)
            else:
                async with httpx.AsyncClient(timeout=20) as cx:
                    rs = (await cx.get(f"{B}/api/agents/builtin/ghost/runs")).json()
                    ck("no stuck 'running' run after a restart",
                       all(r["status"] != "running" for r in rs), str(rs))
                    ck("the orphan says it was interrupted",
                       bool(rs) and "interrupted" in (rs[0].get("error") or ""), str(rs))
        finally:
            proc.send_signal(signal.SIGTERM)
            try: proc.wait(timeout=5)
            except Exception: proc.kill()

        st = Store(DB)
        # It still counts as an attempt (it ran, and spent tokens, before the daemon died) — what
        # it no longer does is sit there as 'running' forever.
        rs = st.list_agent_runs("ghost")
        ck("the orphan is closed out as failed", [r["status"] for r in rs] == ["failed"], str(rs))
        st.con.close()
    finally:
        stub.shutdown()

    # ── a capped run must not count toward the cap that produced it ──────────
    # Past the ceiling, every further trigger fire writes another `capped` row. If those counted,
    # the count would never fall back below the ceiling while the trigger keeps firing, so the
    # agent would stay disabled for 24h after the last ATTEMPT rather than after its last real
    # run — a trigger in a hot loop (the case the cap exists for) would silence its agent for
    # good. A capped run returns before any model call, so it costs nothing to begin with.
    st = Store(DB)
    st.upsert_catalog_agent("hot-loop", "incident", "Take a first look.")
    st.start_agent_run("run_real", "hot-loop", "incident", "d_real", "checkout", "h")
    st.finish_agent_run("run_real", "ok")
    ck("a completed run counts toward the cap", st.agent_runs_today("hot-loop") == 1)

    for i in range(5):
        st.start_agent_run(f"run_capped_{i}", "hot-loop", "incident", "d_cap", "checkout", "h")
        st.finish_agent_run(f"run_capped_{i}", "capped", error="cap reached")
    ck("capped runs do not count toward the cap",
       st.agent_runs_today("hot-loop") == 1, str(st.agent_runs_today("hot-loop")))

    # Deliberately still counted: some failures happen after the model call, so they cost real
    # tokens, and a cost ceiling should err towards counting.
    st.start_agent_run("run_failed", "hot-loop", "incident", "d_fail", "checkout", "h")
    st.finish_agent_run("run_failed", "failed", error="boom")
    ck("failed runs still count", st.agent_runs_today("hot-loop") == 2,
       str(st.agent_runs_today("hot-loop")))
    st.con.close()

    print(f"\n{P} passed, {F} failed")

asyncio.run(main())
sys.exit(1 if F else 0)
