import asyncio, os, time
from _env import remove_db
os.environ["TARES_DB"]="/tmp/otlp.duckdb"; os.environ["TARES_CATALOG"]="/tmp/none.yaml"
remove_db("/tmp/otlp.duckdb")
import httpx
from tares.daemon import make_app
P=F=0
def ck(l,c,d=""):
    global P,F; P+=1 if c else 0; F+=0 if c else 1
    print(("  ok   " if c else "  FAIL ")+l+("" if c else f"  {d}"))

now_ns = str(int(time.time()*1e9))
def ns(off): return str(int(time.time()*1e9) + off)

OTLP = {"resourceLogs": [
  {"resource": {"attributes": [
      {"key":"service.name","value":{"stringValue":"checkout"}},
      {"key":"deployment.environment","value":{"stringValue":"prod"}}]},
   "scopeLogs": [{"scope":{"name":"app","version":"1.0"}, "logRecords": [
      {"timeUnixNano": ns(0), "severityText":"ERROR", "body":{"stringValue":"db pool exhausted"},
       "attributes":[{"key":"http.status","value":{"intValue":"500"}}], "traceId":"abc123"},
      {"timeUnixNano": ns(1000), "severityText":"INFO", "body":{"stringValue":"request ok"}}]}]},
  {"resource": {"attributes": [{"key":"service.name","value":{"stringValue":"payments"}}]},
   "scopeLogs": [{"scope":{}, "logRecords": [
      {"timeUnixNano": ns(2000), "severityText":"WARN", "body":{"stringValue":"slow upstream"}}]}]},
]}

def label_counts(app, source, label):
    """{value: lines} for one label of a source, over all time (the entity listing went, TR-1810)."""
    from datetime import datetime, timezone
    return app.state.store.aggregate([source], None, "count", datetime(2000, 1, 1, tzinfo=timezone.utc),
                                     group_by=label)


async def main():
    app=make_app()
    async with app.router.lifespan_context(app):
        cx=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        # no otlp source yet -> POST /v1/logs auto-provisions one and ingests
        r=await cx.post("/v1/logs", json=OTLP)
        ck("POST /v1/logs -> 200", r.status_code==200, r.text)
        srcs={s["name"]:s for s in (await cx.get("/api/sources")).json()}
        ck("auto-provisioned 'otlp' source (push)", "otlp" in srcs and (srcs["otlp"]["health"] or {}).get("status")=="push", str(list(srcs)))
        ck("otlp default labels: service primary",
           srcs["otlp"]["config"]["labels"]==[{"name":"service","field":"resourceAttributes.service.name","primary":True}], str(srcs["otlp"]["config"]))
        ck("3 events ingested", (srcs["otlp"]["health"] or {}).get("events_total")==3, str((srcs["otlp"]["health"] or {})))

        # entities: service is the primary facet, keyed per service from resource attrs
        sv=label_counts(app, "otlp", "service")
        ck("per-service lines checkout=2, payments=1", sv=={"checkout":2,"payments":1}, str(sv))
        ck("only declared labels are labels (env not declared)", label_counts(app, "otlp", "env") in ({}, {None: 3}), str(label_counts(app, "otlp", "env")))

        # event mapping: text, event_type from severity, intValue decoded into fields
        evs=(await cx.get("/api/sources/otlp/events?limit=10")).json()
        err=[e for e in evs if e["text"]=="db pool exhausted"]
        ck("log body -> text", bool(err), str([e['text'] for e in evs]))
        ck("severityText -> event_type", err and err[0]["event_type"]=="ERROR", str(err[:1]))

        # --- traces ---
        TRACES={"resourceSpans":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"checkout"}}]},
          "scopeSpans":[{"scope":{"name":"app"},"spans":[
            {"name":"GET /pay","traceId":"t1","spanId":"s1","startTimeUnixNano":ns(0),
             "endTimeUnixNano":ns(5_000_000),"status":{"code":"STATUS_CODE_ERROR"}},          # enum-as-string
            {"name":"GET /ok","traceId":"t2","spanId":"s2","startTimeUnixNano":ns(0),
             "endTimeUnixNano":ns(2_000_000),"status":{"code":2}}]}]}]}                        # enum-as-number
        r=await cx.post("/v1/traces", json=TRACES)
        ck("POST /v1/traces -> 200", r.status_code==200, r.text)
        spans=[e for e in (await cx.get("/api/sources/otlp/events?limit=20")).json() if e["event_type"]=="span"]
        ck("2 spans ingested, event_type=span", len(spans)==2, str(len(spans)))
        ck("span text has name + duration + ERROR",
           any("GET /pay" in s["text"] and "ms)" in s["text"] and "ERROR" in s["text"] for s in spans),
           str([s['text'] for s in spans]))

        # --- metrics ---
        METRICS={"resourceMetrics":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"checkout"}}]},
          "scopeMetrics":[{"scope":{},"metrics":[
            {"name":"db_pool_size","unit":"1","gauge":{"dataPoints":[
              {"timeUnixNano":ns(0),"asInt":"20"}]}},
            {"name":"http.server.duration","unit":"ms","histogram":{"dataPoints":[
              {"timeUnixNano":ns(0),"count":"42","sum":1234.5}]}}]}]}]}
        r=await cx.post("/v1/metrics", json=METRICS)
        ck("POST /v1/metrics -> 200", r.status_code==200, r.text)
        mevs={e["event_type"]:e for e in (await cx.get("/api/sources/otlp/events?limit=40")).json()}
        ck("gauge metric: event_type=name, value in text", "db_pool_size" in mevs and "20" in mevs["db_pool_size"]["text"], str(list(mevs)))
        ck("histogram metric ingested (event_type=metric name)", "http.server.duration" in mevs, str(list(mevs)))

        # multiple otlp sources -> header required
        await cx.post("/api/sources", json={"name":"otlp2","connector":"otlp","config":{"labels":[{"name":"svc","field":"service.name","primary":True}]}})
        r=await cx.post("/v1/logs", json=OTLP)
        ck("ambiguous (2 otlp sources) -> 400", r.status_code==400, r.text[:80])
        r=await cx.post("/v1/logs", json=OTLP, headers={"X-Tares-Source":"otlp2"})
        ck("X-Tares-Source routes to the named source", r.status_code==200, r.text)
        await cx.aclose()
    print(f"\n{P} passed, {F} failed"); raise SystemExit(1 if F else 0)
asyncio.run(main())
