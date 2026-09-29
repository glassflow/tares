"""Tares agents: the per-agent round cap (TR-160).

Against a stub Anthropic endpoint: the default cap is 6, an agent with external MCP servers gets
12, an explicit `max_rounds` wins and is validated (1..24), the effective cap is recorded on every
run, a run that spends its budget gets one tools-off call to conclude, and when even that yields
nothing the run ends `exhausted` with the last text kept as a partial note. Catalog import and
export round-trip the field.
"""
import asyncio, json, os, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

SEED = "/tmp/agent_rounds_catalog.yaml"
with open(SEED, "w") as fh:
    fh.write(
        "sources:\n  - name: evt\n    connector: webhook\n    poll: 5s\n"
        "    config:\n      labels:\n        - name: service\n          field: service\n"
        "          primary: true\n"
        "triggers:\n  - name: incident\n    sources: [evt]\n    key_field: service\n    cooldown: 1s\n"
        "    condition:\n      aggregate: count\n      predicate: '>= 2'\n      window: 1m\n"
        "agents:\n"
        "  - name: capped-3\n    trigger: incident\n    prompt: keep reading\n    max_rounds: 3\n"
        "  - name: no-cap\n    trigger: incident\n    prompt: keep reading\n")

DB, PORT, STUB_PORT = "/tmp/agent_rounds.duckdb", "8816", "8817"

# ── stub Anthropic ────────────────────────────────────────────────────────────
# Mode "greedy": always asks for another read while tools are allowed; on the tools-off call it
# concludes. Mode "mute": same, but the tools-off call returns no text (run ends exhausted).
MODE = {"value": "greedy"}
_calls: list[dict] = []


class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _calls.append(body)
        tools_off = (body.get("tool_choice") or {}).get("type") == "none"
        if not tools_off:
            content = [{"type": "text", "text": f"still looking, call {len(_calls)}"},
                       {"type": "tool_use", "id": f"tu_{len(_calls)}", "name": "read",
                        "input": {"selector": {"service": "checkout"}, "window": "1h"}}]
        elif MODE["value"] == "greedy":
            content = [{"type": "text", "text": "concluded on the final call"}]
        else:
            content = []
        out = json.dumps({"content": content, "model": body.get("model")}).encode()
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


async def _until(fn, tries=80):
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
            lst = (await cx.get(f"{B}/api/agents/builtin")).json()
            ck("defaults advertised", lst.get("default_max_rounds") == 6
               and lst.get("default_max_rounds_with_mcp") == 12
               and lst.get("max_rounds_limit") == 24, str({k: lst.get(k) for k in lst if "round" in k}))
            by = {a["name"]: a for a in lst["agents"]}
            ck("catalog import: explicit max_rounds kept", by["capped-3"]["max_rounds"] == 3
               and by["capped-3"]["effective_max_rounds"] == 3, str(by.get("capped-3")))
            ck("catalog import: no field means default 6", by["no-cap"]["max_rounds"] is None
               and by["no-cap"]["effective_max_rounds"] == 6, str(by.get("no-cap")))

            # ── validation ───────────────────────────────────────────────────
            r = await cx.post(f"{B}/api/agents/builtin", json={
                "name": "too-many", "trigger": "incident", "prompt": "x", "max_rounds": 25})
            ck("max_rounds 25 rejected (400)", r.status_code == 400, r.text)
            r = await cx.post(f"{B}/api/agents/builtin", json={
                "name": "zero", "trigger": "incident", "prompt": "x", "max_rounds": 0})
            ck("max_rounds 0 rejected (400)", r.status_code == 400, r.text)
            r = await cx.post(f"{B}/api/agents/builtin", json={
                "name": "explicit-9", "trigger": "incident", "prompt": "x", "max_rounds": 9})
            ck("max_rounds 9 accepted (201)", r.status_code == 201, r.text)

            # ── effective 12 once an MCP server is attached ──────────────────
            r = await cx.post(f"{B}/api/mcp-servers", json={
                "name": "ext", "url": "http://127.0.0.1:1/mcp"})
            ck("mcp server registered", r.status_code == 201, r.text)
            r = await cx.post(f"{B}/api/agents/builtin", json={
                "name": "with-mcp", "trigger": "incident", "prompt": "x", "mcp_servers": ["ext"]})
            ck("agent with mcp server created", r.status_code == 201, r.text)
            lst = (await cx.get(f"{B}/api/agents/builtin")).json()
            by = {a["name"]: a for a in lst["agents"]}
            ck("with mcp servers, effective default is 12",
               by["with-mcp"]["max_rounds"] is None and by["with-mcp"]["effective_max_rounds"] == 12,
               str(by["with-mcp"]))
            ck("explicit 9 stays 9", by["explicit-9"]["effective_max_rounds"] == 9, str(by["explicit-9"]))
            # update to an explicit value on the mcp agent wins over the 12
            r = await cx.put(f"{B}/api/agents/builtin/with-mcp", json={
                "name": "with-mcp", "trigger": "incident", "prompt": "x", "mcp_servers": ["ext"],
                "max_rounds": 4})
            ck("update sets max_rounds", r.status_code == 200, r.text)
            by = {a["name"]: a for a in (await cx.get(f"{B}/api/agents/builtin")).json()["agents"]}
            ck("explicit 4 wins over the mcp default", by["with-mcp"]["effective_max_rounds"] == 4, str(by["with-mcp"]))

            # ── export round-trips the field ─────────────────────────────────
            y = (await cx.get(f"{B}/api/catalog/export")).text
            ck("export carries max_rounds for capped-3", "max_rounds: 3" in y, y[-600:])
            ck("export omits max_rounds when unset",
               y.count("max_rounds") == 3, str(y.count("max_rounds")))   # capped-3, explicit-9, with-mcp

            # ── a run held to the cap: greedy model, tools-off call concludes ─
            _calls.clear()
            MODE["value"] = "greedy"
            r = await cx.post(f"{B}/api/agents/builtin/capped-3/enable")
            ck("enable capped-3", r.status_code == 200, r.text)
            for i in range(3):
                await cx.post(f"{B}/ingest/evt", json={"service": "checkout", "msg": f"500 #{i}"})

            async def _ran():
                rs = (await cx.get(f"{B}/api/agents/builtin/capped-3/runs")).json()
                return bool(rs) and rs[0]["status"] != "running"
            ck("capped-3 ran", await _until(_ran), "no finished run")
            runs = (await cx.get(f"{B}/api/agents/builtin/capped-3/runs")).json()
            run = runs[0]
            ck("run records the effective cap (3)", run.get("max_rounds") == 3, str(run))
            ck("run used exactly 3 rounds", run["rounds"] == 3, str(run))
            ck("the final tools-off call concluded (status ok)", run["status"] == "ok", str(run))
            ck("finding is the tools-off conclusion", run["finding"] == "concluded on the final call", str(run))
            ck("3 tool rounds + 1 tools-off call = 4 model calls", len(_calls) == 4, str(len(_calls)))
            ck("final call disabled tools via tool_choice none",
               (_calls[-1].get("tool_choice") or {}).get("type") == "none" and "tools" in _calls[-1],
               str(_calls[-1].get("tool_choice")))
            await cx.post(f"{B}/api/agents/builtin/capped-3/disable")

            # ── exhausted: the tools-off call returns nothing ────────────────
            _calls.clear()
            MODE["value"] = "mute"
            r = await cx.post(f"{B}/api/agents/builtin/explicit-9/enable")
            ck("enable explicit-9", r.status_code == 200, r.text)
            await asyncio.sleep(1.2)   # past the 1s trigger cooldown
            for i in range(3):
                await cx.post(f"{B}/ingest/evt", json={"service": "checkout", "msg": f"500 again #{i}"})

            async def _ran2():
                rs = (await cx.get(f"{B}/api/agents/builtin/explicit-9/runs")).json()
                return bool(rs) and rs[0]["status"] != "running"
            ck("explicit-9 ran", await _until(_ran2), "no finished run")
            run = (await cx.get(f"{B}/api/agents/builtin/explicit-9/runs")).json()[0]
            ck("status is exhausted", run["status"] == "exhausted", str(run))
            ck("rounds/max recorded as 9/9", run["rounds"] == 9 and run.get("max_rounds") == 9, str(run))
            ck("partial note kept as the finding text", run.get("finding") == "still looking, call 9", str(run))
            ck("error names the fix", "raise max rounds" in (run.get("error") or ""), str(run.get("error")))
            ck("10 model calls (9 + the tools-off one)", len(_calls) == 10, str(len(_calls)))
            # an exhausted run does not write a finding onto the timeline
            fnd = (await cx.get(f"{B}/read", params={"selector": json.dumps({"service": "checkout"}),
                                                     "sources": "findings", "window": "1h"}))
            ck("no finding event written for the exhausted run",
               fnd.status_code != 200 or "still looking" not in fnd.text, fnd.text[:200])
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
        stub.shutdown()
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
