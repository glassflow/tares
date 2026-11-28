"""OTLP gRPC receiver test. Skips cleanly if the optional [otlp-grpc] deps aren't installed."""
import asyncio, os, time
from _env import remove_db
os.environ["TARES_DB"] = "/tmp/otlpg.duckdb"
os.environ["TARES_CATALOG"] = "/tmp/none.yaml"
os.environ["TARES_OTLP_GRPC_PORT"] = "14317"
remove_db("/tmp/otlpg.duckdb")
try:
    import grpc
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2 as lpb, logs_service_pb2_grpc as lgrpc
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2 as tpb, trace_service_pb2_grpc as tgrpc
    from opentelemetry.proto.logs.v1 import logs_pb2
    from opentelemetry.proto.trace.v1 import trace_pb2
    from opentelemetry.proto.common.v1 import common_pb2
    from opentelemetry.proto.resource.v1 import resource_pb2
except ImportError:
    print("[otlp-grpc] deps not installed — skipping gRPC test")
    raise SystemExit(0)
import httpx
from tares.daemon import make_app

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

def _res(svc):
    return resource_pb2.Resource(attributes=[
        common_pb2.KeyValue(key="service.name", value=common_pb2.AnyValue(string_value=svc))])

def label_counts(app, source, label):
    """{value: lines} for one label of a source, over all time (the entity listing went, TR-1810)."""
    from datetime import datetime, timezone
    return app.state.store.aggregate([source], None, "count", datetime(2000, 1, 1, tzinfo=timezone.utc),
                                     group_by=label)


async def main():
    app = make_app()
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.4)
        ns = int(time.time() * 1e9)
        logreq = lpb.ExportLogsServiceRequest(resource_logs=[logs_pb2.ResourceLogs(
            resource=_res("checkout"), scope_logs=[logs_pb2.ScopeLogs(log_records=[
                logs_pb2.LogRecord(time_unix_nano=ns, severity_text="ERROR",
                                   body=common_pb2.AnyValue(string_value="grpc boom"))])])])
        spanreq = tpb.ExportTraceServiceRequest(resource_spans=[trace_pb2.ResourceSpans(
            resource=_res("checkout"), scope_spans=[trace_pb2.ScopeSpans(spans=[
                trace_pb2.Span(name="GET /pay", trace_id=b"0" * 16, span_id=b"0" * 8,
                               start_time_unix_nano=ns, end_time_unix_nano=ns + 3_000_000,
                               status=trace_pb2.Status(code=trace_pb2.Status.STATUS_CODE_ERROR))])])])
        async with grpc.aio.insecure_channel("localhost:14317") as ch:
            r = await lgrpc.LogsServiceStub(ch).Export(logreq)
            ck("gRPC LogsService.Export returns a response", r is not None)
            await tgrpc.TraceServiceStub(ch).Export(spanreq)
        await asyncio.sleep(0.4)
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        ck("gRPC auto-provisioned 'otlp' source",
           any(s["name"] == "otlp" for s in (await cx.get("/api/sources")).json()))
        evs = (await cx.get("/api/sources/otlp/events?limit=10")).json()
        ck("gRPC log ingested, keyed by service",
           any(e["text"] == "grpc boom" and e["event_type"] == "ERROR" and e["key"] == "checkout" for e in evs),
           str([e["text"] for e in evs]))
        ck("gRPC span ingested (event_type=span, ERROR)",
           any(e["event_type"] == "span" and "GET /pay" in e["text"] and "ERROR" in e["text"] for e in evs),
           str([(e["event_type"], e["text"]) for e in evs]))
        sv = label_counts(app, "otlp", "service")
        ck("service is a label from gRPC data", "checkout" in sv, str(sv))
        await cx.aclose()
    print(f"\n{P} passed, {F} failed"); raise SystemExit(1 if F else 0)

asyncio.run(main())
