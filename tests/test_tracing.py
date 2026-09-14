"""Agent tracing (tares/tracing.py): configuration resolution, the span helpers, and an
end-to-end run exported over OTLP/HTTP to a stub receiver.

Part 1 is in-process: presets and precedence, the OpenInference/gen_ai attributes the helpers
write, the provider cache rebuilding when the configuration changes.
Part 2 boots the daemon against the stub Anthropic endpoint from test_agents.py and a stub OTLP
receiver, runs an agent, and asserts the trace that arrives: service.name per agent, a root
AGENT span, an LLM span with token usage, a TOOL span. Then switches tracing off through the
settings API and checks the next run exports nothing.
"""
import asyncio, json, os, signal, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


class FakeStore:
    def __init__(self):
        self.d = {}
    def get_setting(self, k):
        return self.d.get(k)
    def set_setting(self, k, v):
        if v is None:
            self.d.pop(k, None)
        else:
            self.d[k] = v


def _clear_env():
    for k in list(os.environ):
        if k.startswith("TARES_TRACING") or k == "TARES_INSTANCE_NAME":
            del os.environ[k]


# ── part 1: in-process ────────────────────────────────────────────────────────
def part1():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from tares import tracing as T

    _clear_env()
    st = FakeStore()

    cfg = T.resolve(st)
    ck("off by default", not cfg.enabled and not cfg.active, str(cfg))
    ck("no provider named and no endpoint -> the rius preset",
       cfg.provider == "rius" and cfg.endpoint == T.RIUS_ENDPOINT
       and cfg.sources["endpoint"] == "preset", str(cfg))

    os.environ["TARES_TRACING_API_KEY"] = "gf_env"
    os.environ["TARES_TRACING_ENABLED"] = "true"
    os.environ["TARES_INSTANCE_NAME"] = "cell-a"
    cfg = T.resolve(st)
    ck("env key becomes a bearer header", cfg.headers.get("authorization") == "Bearer gf_env", str(cfg.headers))
    ck("env switch turns it on", cfg.enabled and cfg.active, str(cfg))
    ck("instance from env", cfg.instance == "cell-a", cfg.instance)
    ck("key source reported as env", cfg.sources["api_key"] == "env:TARES_TRACING_API_KEY", str(cfg.sources))

    st.set_setting("tracing_api_key", "gf_console")
    st.set_setting("tracing_enabled", "0")
    cfg = T.resolve(st)
    ck("console key wins over env", cfg.headers.get("authorization") == "Bearer gf_console", str(cfg.headers))
    ck("console switch wins over env", not cfg.enabled, str(cfg))

    st.set_setting("tracing_enabled", "1")
    st.set_setting("tracing_endpoint", "http://collector:4318")
    st.set_setting("tracing_headers", "x-langfuse-public=pk, x-langfuse-secret=sk")
    cfg = T.resolve(st)
    ck("an endpoint without a provider means otlp", cfg.provider == "otlp", cfg.provider)
    ck("headers parsed", cfg.headers.get("x-langfuse-public") == "pk"
       and cfg.headers.get("x-langfuse-secret") == "sk", str(cfg.headers))
    st.set_setting("tracing_headers", "authorization=Basic abc")
    ck("explicit authorization header wins over the key",
       T.resolve(st).headers.get("authorization") == "Basic abc", str(T.resolve(st).headers))
    st.set_setting("tracing_headers", "x-langfuse-public=pk, x-langfuse-secret=sk")
    s = T.status(st)
    ck("status never carries secrets", "gf_console" not in json.dumps(s) and "sk" not in json.dumps(s.get("headers_source")), json.dumps(s))
    ck("status reports key configured + stored", s["key_configured"] and s["key_stored"], str(s))

    # ── the helpers ──────────────────────────────────────────────────────────
    exporters = []
    def factory(cfg):
        ex = InMemorySpanExporter(); exporters.append(ex); return ex
    tr = T.Tracing(st, exporter_factory=factory)
    tracer = tr.tracer_for("first-look")
    ck("tracer built when active", tracer is not None)
    with T.run_span(tracer, "first-look", session="checkout",
                    attributes={"tares.run_id": "run_1", "tares.dispatch_id": ""}) as obs:
        obs.set_input("timeline")
        with T.generation(tracer, "claude-sonnet-4-6",
                          [{"role": "user", "content": "hi"}], {"max_tokens": 10}) as gen:
            gen.set_output([{"type": "text", "text": "ok"},
                            {"type": "tool_use", "id": "t1", "name": "read", "input": {"a": 1}}])
            gen.set_usage({"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 7})
            gen.set_response_model("claude-sonnet-4-6-x")
            gen.set_finish_reason("tool_use")
        with T.tool_span(tracer, "read", {"selector": {"service": "checkout"}}) as tobs:
            tobs.set_output("payload")
        obs.set_output("the finding")
        obs.set_attribute("tares.status", "ok")
    tr.flush()
    spans = exporters[0].get_finished_spans()
    by = {s.name: s for s in spans}
    ck("three spans: run, chat, tool", len(spans) == 3 and {"first-look", "chat claude-sonnet-4-6", "read"} <= set(by), str(list(by)))
    root = by["first-look"]
    ck("service.name is <instance>/<agent>", root.resource.attributes.get("service.name") == "cell-a/first-look",
       str(root.resource.attributes))
    ck("root span is AGENT kind", root.attributes.get("openinference.span.kind") == "AGENT", str(root.attributes))
    ck("root carries input and output", root.attributes.get("input.value") == "timeline"
       and root.attributes.get("output.value") == "the finding", str(root.attributes))
    ck("empty attributes are dropped", "tares.dispatch_id" not in root.attributes, str(root.attributes))
    ck("session.id on every span", all(s.attributes.get("session.id") == "checkout" for s in spans),
       str([s.attributes.get("session.id") for s in spans]))
    ck("children nest under the root", all(s.parent and s.parent.span_id == root.context.span_id
                                           for s in spans if s is not root))
    llm = by["chat claude-sonnet-4-6"]
    a = llm.attributes
    ck("llm span kind + provider + model", a.get("openinference.span.kind") == "LLM"
       and a.get("gen_ai.provider.name") == "anthropic" and a.get("gen_ai.request.model") == "claude-sonnet-4-6", str(a))
    ck("llm usage", a.get("gen_ai.usage.input_tokens") == 100 and a.get("gen_ai.usage.output_tokens") == 50
       and a.get("gen_ai.usage.cache_read_input_tokens") == 7, str(a))
    ck("llm response model + finish reason", a.get("gen_ai.response.model") == "claude-sonnet-4-6-x"
       and tuple(a.get("gen_ai.response.finish_reasons")) == ("tool_use",), str(a))
    inp = json.loads(a["gen_ai.input.messages"]); out = json.loads(a["gen_ai.output.messages"])
    ck("input messages in the gen_ai shape", inp == [{"role": "user", "parts": [{"type": "text", "content": "hi"}]}], str(inp))
    ck("tool_use block becomes a tool_call part", out[0]["role"] == "assistant"
       and out[0]["parts"][1] == {"type": "tool_call", "id": "t1", "name": "read", "arguments": {"a": 1}}, str(out))
    ck("request parameter recorded", a.get("gen_ai.request.max_tokens") == 10, str(a))
    tool = by["read"]
    ck("tool span kind + name", tool.attributes.get("openinference.span.kind") == "TOOL"
       and tool.attributes.get("gen_ai.tool.name") == "read", str(tool.attributes))
    # the provider attribute follows the adapter's kind (TR-305)
    with T.generation(tracer, "gpt-x", [{"role": "user", "content": "hi"}], provider="openai") as gen:
        gen.set_usage({"input_tokens": 1, "output_tokens": 1})
    tr.flush()
    oai = next(s for s in exporters[0].get_finished_spans() if s.name == "chat gpt-x")
    ck("an OpenAI-format call is traced as provider openai", oai.attributes.get("gen_ai.provider.name") == "openai", str(oai.attributes))
    ck("tool input serialised", json.loads(tool.attributes["input.value"]) == {"selector": {"service": "checkout"}}, str(tool.attributes))

    # a failed tool call is flagged on its span, but does not fail the trace
    with T.run_span(tracer, "first-look", session="k") as obs:
        with T.tool_span(tracer, "query", {}) as tobs:
            tobs.tool_error("tool error: query needs a key")
    tr.flush()
    got = exporters[0].get_finished_spans()[-2:]
    tspan = next(s for s in got if s.name == "query"); rspan = next(s for s in got if s.name == "first-look")
    ck("tool error: flag + text on the tool span", tspan.attributes.get("tares.tool_error") is True
       and "needs a key" in tspan.attributes.get("output.value", ""), str(tspan.attributes))
    ck("tool error: no ERROR status on the tool span", tspan.status.status_code.name != "ERROR", str(tspan.status))
    ck("tool error: run span stays unset", rspan.status.status_code.name == "UNSET", str(rspan.status))

    # a failed run marks the span
    with T.run_span(tracer, "first-look", session="k") as obs:
        obs.error("boom")
    tr.flush()
    last = exporters[0].get_finished_spans()[-1]
    ck("error sets ERROR status", last.status.status_code.name == "ERROR" and "boom" in (last.status.description or ""), str(last.status))

    # an exception inside the block is recorded and re-raised
    try:
        with T.run_span(tracer, "x", session="k"):
            raise RuntimeError("bad")
    except RuntimeError:
        raised = True
    else:
        raised = False
    tr.flush()
    last = exporters[0].get_finished_spans()[-1]
    ck("exception re-raised and recorded", raised and last.status.status_code.name == "ERROR", str(last.status))

    # None tracer: everything is a no-op
    with T.run_span(None, "x", session="k") as obs:
        obs.set_input("a"); obs.error("b")
        with T.generation(None, "m", "hi") as gen:
            gen.set_usage({"input_tokens": 1}); gen.record_first_token()
        with T.tool_span(None, "t", {}) as tobs:
            tobs.set_output("o")
    ck("no tracer: helpers are no-ops", True)

    # one provider per agent, cached
    t2 = tr.tracer_for("second"); t1b = tr.tracer_for("first-look")
    ck("providers cached per agent", len(exporters) == 2 and t1b is not None and t2 is not None, str(len(exporters)))

    # config change rebuilds; switching off tears down
    st.set_setting("tracing_endpoint", "http://other:4318")
    tr.tracer_for("first-look")
    ck("changed endpoint rebuilds the providers", len(exporters) == 3, str(len(exporters)))
    st.set_setting("tracing_enabled", "0")
    ck("switched off -> no tracer", tr.tracer_for("first-look") is None)
    ck("switched off -> cache emptied", not tr._providers)
    st.set_setting("tracing_enabled", "1")
    ck("switched on again -> tracer without a restart", tr.tracer_for("first-look") is not None)
    tr.shutdown()
    _clear_env()


# ── part 2: end to end through the daemon ────────────────────────────────────
SEED = "/tmp/tracing_catalog.yaml"
DB, PORT, STUB_PORT, OTLP_PORT = "/tmp/tracing.duckdb", "8816", "8817", "8818"
FINDING = "checkout is returning 500s; roll back."
_calls = []
_received = []   # decoded ExportTraceServiceRequest messages


class AnthropicStub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _calls.append(body)
        # the stub is shared across runs: one tool round per run, then the conclusion
        n = sum(1 for c in _calls if c["messages"][0] == body["messages"][0])
        if n == 1:
            content = [{"type": "tool_use", "id": "tu_1", "name": "read",
                        "input": {"selector": {"service": "checkout"}, "window": "1h"}}]
        else:
            content = [{"type": "text", "text": FINDING}]
        out = json.dumps({"content": content, "model": body.get("model"), "stop_reason": "end_turn",
                          "usage": {"input_tokens": 100, "output_tokens": 50}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class OtlpStub(BaseHTTPRequestHandler):
    def do_POST(self):
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        raw = self.rfile.read(int(self.headers["content-length"]))
        req = ExportTraceServiceRequest(); req.ParseFromString(raw)
        _received.append((self.path, dict(self.headers), req))
        self.send_response(200)
        self.send_header("content-type", "application/x-protobuf")
        self.send_header("content-length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


def _spans():
    """Flatten what the receiver got: [(service.name, span)]."""
    out = []
    for _path, _h, req in _received:
        for rs in req.resource_spans:
            svc = next((kv.value.string_value for kv in rs.resource.attributes if kv.key == "service.name"), "")
            for ss in rs.scope_spans:
                for sp in ss.spans:
                    out.append((svc, sp))
    return out


def _attr(span, key):
    for kv in span.attributes:
        if kv.key == key:
            v = kv.value
            if v.HasField("string_value"): return v.string_value
            if v.HasField("int_value"): return v.int_value
            if v.HasField("array_value"): return [x.string_value for x in v.array_value.values]
    return None


async def _wait(url, tries=80):
    for _ in range(tries):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=2)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


async def _until(fn, tries=60):
    for _ in range(tries):
        if await fn():
            return True
        await asyncio.sleep(0.5)
    return False


async def part2():
    with open(SEED, "w") as fh:
        fh.write(
            "sources:\n  - name: evt\n    connector: webhook\n    poll: 5s\n"
            "    config:\n      labels:\n        - name: service\n          field: service\n"
            "          primary: true\n"
            "views:\n  - name: svc\n    key_field: service\n    sources: [evt]\n"
            "triggers:\n  - name: incident\n    view: svc\n    cooldown: 1s\n"
            "    condition:\n      aggregate: count\n      predicate: '>= 2'\n      window: 1m\n")
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    for srv in (HTTPServer(("127.0.0.1", int(STUB_PORT)), AnthropicStub),
                HTTPServer(("127.0.0.1", int(OTLP_PORT)), OtlpStub)):
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    env = {**os.environ, "TARES_DB": DB, "TARES_CATALOG": SEED, "TARES_PORT": PORT,
           "TARES_OTLP_GRPC_PORT": "off", "ANTHROPIC_API_KEY": "sk-test",
           "TARES_ANTHROPIC_BASE": f"http://127.0.0.1:{STUB_PORT}",
           "TARES_TRIGGER_DEBOUNCE_SECONDS": "0",
           # the generic provider: any OTLP/HTTP endpoint, a key as bearer, on from the env
           "TARES_TRACING_ENABLED": "1", "TARES_TRACING_PROVIDER": "otlp",
           "TARES_TRACING_ENDPOINT": f"http://127.0.0.1:{OTLP_PORT}",
           "TARES_TRACING_API_KEY": "gf_test", "TARES_INSTANCE_NAME": "local",
           "OTEL_BSP_SCHEDULE_DELAY": "200"}
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{PORT}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient(timeout=20) as cx:
            s = (await cx.get(f"{B}/api/settings/tracing")).json()
            ck("settings: on from env, provider otlp, endpoint from env",
               s["enabled"] and s["active"] and s["provider"] == "otlp"
               and s["endpoint_source"] == "env:TARES_TRACING_ENDPOINT", str(s))
            ck("settings never return the key", "gf_test" not in json.dumps(s), json.dumps(s))

            r = await cx.post(f"{B}/api/agents/builtin", json={
                "name": "first-look", "trigger": "incident", "prompt": "Take a first look."})
            ck("create agent -> 201", r.status_code == 201, r.text)
            await cx.post(f"{B}/api/agents/builtin/first-look/enable")
            for i in range(3):
                await cx.post(f"{B}/ingest/evt", json={"service": "checkout", "msg": f"500 #{i}"})

            async def _ran():
                rs = (await cx.get(f"{B}/api/agents/builtin/first-look/runs")).json()
                return bool(rs) and rs[0]["status"] != "running"
            ck("a run completed", await _until(_ran), "no completed run")
            run = (await cx.get(f"{B}/api/agents/builtin/first-look/runs")).json()[0]
            ck("run status ok (tracing did not get in the way)", run.get("status") == "ok", str(run))

            async def _got():
                return any(sp.name == "first-look" for _s, sp in _spans())
            ck("trace exported to the OTLP endpoint", await _until(_got), str(len(_received)))
            path, headers, _ = _received[0]
            ck("posted to /v1/traces", path == "/v1/traces", path)
            ck("bearer header carries the key", headers.get("authorization") == "Bearer gf_test", str(headers))
            spans = _spans()
            root = next((sp for svc, sp in spans if sp.name == "first-look"), None)
            svc = next((svc for svc, sp in spans if sp.name == "first-look"), None)
            ck("service.name is <instance>/<agent>", svc == "local/first-look", str(svc))
            ck("root span: AGENT kind, run id, status ok",
               root is not None and _attr(root, "openinference.span.kind") == "AGENT"
               and _attr(root, "tares.run_id") == run["id"] and _attr(root, "tares.status") == "ok",
               str([(kv.key, kv.value) for kv in root.attributes]) if root else "no root")
            ck("root output is the finding", root is not None and _attr(root, "output.value") == FINDING)
            ck("root session is the entity key", root is not None and _attr(root, "session.id") == "checkout")
            ck("root carries the instance and agent as span attributes",
               root is not None and _attr(root, "tares.instance") == "local" and _attr(root, "tares.agent") == "first-look")
            ck("root records cost + calls", root is not None and _attr(root, "tares.model_calls") == 2)
            llms = [sp for svc, sp in spans if _attr(sp, "openinference.span.kind") == "LLM"]
            ck("one LLM span per model call", len(llms) == 2, str(len(llms)))
            ck("LLM spans carry usage", all(_attr(sp, "gen_ai.usage.input_tokens") == 100
                                           and _attr(sp, "gen_ai.usage.output_tokens") == 50 for sp in llms))
            ck("LLM spans carry the finish reason", all(_attr(sp, "gen_ai.response.finish_reasons") == ["end_turn"] for sp in llms))
            tools = [sp for svc, sp in spans if _attr(sp, "openinference.span.kind") == "TOOL"]
            ck("one TOOL span, named read", len(tools) == 1 and tools[0].name == "read"
               and _attr(tools[0], "gen_ai.tool.name") == "read", str([t.name for t in tools]))
            ck("tool span has the payload as output", bool(tools) and "checkout" in (_attr(tools[0], "output.value") or ""))
            ck("children share the root trace", all(sp.trace_id == root.trace_id for _s, sp in spans))

            # ── switch off through the API; the next run exports nothing ────
            r = await cx.put(f"{B}/api/settings/tracing", json={"enabled": False})
            ck("PUT enabled=false -> off", r.status_code == 200 and not r.json()["enabled"]
               and r.json()["enabled_source"] == "console", r.text[:200])
            before = len(_spans())
            await asyncio.sleep(1.2)
            for i in range(3):
                await cx.post(f"{B}/ingest/evt", json={"service": "checkout", "msg": f"500 again #{i}"})
            async def _ran2():
                rs = (await cx.get(f"{B}/api/agents/builtin/first-look/runs")).json()
                return len(rs) >= 2 and rs[0]["status"] != "running"
            ck("a second run completed", await _until(_ran2), "no second run")
            await asyncio.sleep(1.0)
            ck("no spans exported while off", len(_spans()) == before, f"{before} -> {len(_spans())}")

            # ── back on, and a stored key/provider round trip ────────────────
            r = await cx.put(f"{B}/api/settings/tracing", json={"enabled": True, "api_key": "gf_console"})
            ck("PUT enabled=true + key -> on, key stored", r.status_code == 200 and r.json()["active"]
               and r.json()["key_stored"] and r.json()["key_source"] == "console", r.text[:200])
            r = await cx.put(f"{B}/api/settings/tracing", json={"provider": "bogus"})
            ck("unknown provider rejected", r.status_code == 400, r.text[:200])
            r = await cx.put(f"{B}/api/settings/tracing", json={"endpoint": "collector:4318"})
            ck("bad endpoint rejected", r.status_code == 400, r.text[:200])
            r = await cx.put(f"{B}/api/settings/tracing", json={"api_key": ""})
            ck("empty key clears the stored one (env takes over)",
               r.status_code == 200 and not r.json()["key_stored"]
               and r.json()["key_source"] == "env:TARES_TRACING_API_KEY", r.text[:200])
            await asyncio.sleep(1.2)
            for i in range(3):
                await cx.post(f"{B}/ingest/evt", json={"service": "checkout", "msg": f"500 third #{i}"})
            async def _ran3():
                rs = (await cx.get(f"{B}/api/agents/builtin/first-look/runs")).json()
                return len(rs) >= 3 and rs[0]["status"] != "running"
            ck("a third run completed", await _until(_ran3), "no third run")
            async def _more():
                return len(_spans()) > before
            ck("spans flow again after switching back on", await _until(_more), f"{before} -> {len(_spans())}")
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    part1()
    asyncio.run(part2())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
