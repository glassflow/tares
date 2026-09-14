"""The daemon's /metrics endpoint (tares/metrics.py, TR-308): public, Prometheus text format,
and every hook counted where it fires: ingest, polls, source state, agent runs, model usage,
trigger evaluation, storage gauges, event loop lag.

Run: .venv/bin/python tests/test_metrics.py   (in-process daemon, no external services)
"""
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = "/tmp/metrics_test.duckdb"
for pth in (DB, DB + ".wal"):
    if os.path.exists(pth):
        os.remove(pth)
os.environ["TARES_DB"] = DB
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_AUTH_TOKEN"] = "metrics-test-token"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "TARES_CATALOG"):
    os.environ.pop(var, None)

import httpx

from tares import metrics

PASS = FAIL = 0


def ck(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


def sample(body: str, name: str, **labels) -> float | None:
    """The value of one sample, matching every given label."""
    for line in body.splitlines():
        if not line.startswith(name + "{") and not line.startswith(name + " "):
            continue
        m = re.match(r"^(\S+?)(\{(.*)\})? (\S+)$", line)
        if not m:
            continue
        got = dict(re.findall(r'(\w+)="([^"]*)"', m.group(3) or ""))
        if all(got.get(k) == v for k, v in labels.items()):
            return float(m.group(4))
    return None


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     timeout=20) as cx:
            store = app.state.store
            print("== the endpoint ==")
            r = await cx.get("/metrics")
            ck("public, no token needed", r.status_code == 200, r.text[:200])
            ck("prometheus text format", r.headers["content-type"].startswith("text/plain") and "# HELP tares_" in r.text)
            ck("build info", sample(r.text, "tares_info") == 1.0)
            ck("storage gauges present", sample(r.text, "tares_db_bytes") is not None and sample(r.text, "tares_ingest_paused") == 0.0)
            r2 = await cx.get("/api/sources")
            ck("the API itself still needs the token", r2.status_code == 401, str(r2.status_code))

            print("== ingest, sources, triggers ==")
            store.upsert_catalog_source("evt", "webhook", "webhook", "5s",
                                        {"labels": [{"name": "service", "field": "service", "primary": True}]},
                                        ingest_key="ik_evt")
            store.upsert_catalog_view("svc", "service", ["evt"])
            store.upsert_catalog_trigger("errors", "svc", {"field": "service", "aggregate": "count", "predicate": ">= 2", "window": "5m"}, {}, "5m")
            app.state.runtime.reload_catalog()
            for i in range(3):
                r = await cx.post("/ingest/ik_evt", json={"service": "checkout", "message": f"e{i}"},
                                  headers={"Authorization": "Bearer metrics-test-token"})
                assert r.status_code == 202, r.text
            body = (await cx.get("/metrics")).text
            ck("events counted per source", sample(body, "tares_events_ingested_total", source="evt") == 3.0, str(sample(body, "tares_events_ingested_total", source="evt")))
            ck("a push source reads as push", sample(body, "tares_source_state", source="evt", state="push") == 1.0 and sample(body, "tares_source_state", source="evt", state="error") == 0.0)
            ck("trigger passes counted and timed", (sample(body, "tares_trigger_evaluations_total") or 0) >= 3 and sample(body, "tares_trigger_evaluation_seconds_count") is not None)
            ck("the trigger fired once and was counted", sample(body, "tares_trigger_firings_total", trigger="errors") == 1.0, str(sample(body, "tares_trigger_firings_total", trigger="errors")))
            ck("stored events gauge", (sample(body, "tares_events_stored") or 0) >= 3)

            print("== a poll source that fails ==")
            store.upsert_catalog_source("bad", "http_poll", "http_poll", "1s",
                                        {"url": "http://127.0.0.1:1/nothing", "labels": [{"name": "service", "field": "service", "primary": True}]})
            app.state.runtime.reload_catalog()
            for _ in range(60):
                await asyncio.sleep(0.1)
                body = (await cx.get("/metrics")).text
                if (sample(body, "tares_polls_total", source="bad", outcome="error") or 0) >= 1:
                    break
            ck("poll errors counted and the source reads as error",
               (sample(body, "tares_polls_total", source="bad", outcome="error") or 0) >= 1
               and sample(body, "tares_source_state", source="bad", state="error") == 1.0, body[:0])
            store.delete_catalog_source("bad") if hasattr(store, "delete_catalog_source") else None
            app.state.runtime.reload_catalog()
            body = (await cx.get("/metrics")).text
            ck("a removed source drops out of the state gauge", sample(body, "tares_source_state", source="bad", state="error") is None)

            print("== model usage and agent runs ==")
            store.record_model_usage("agent", "sre", "run_1", "gpt-4.1", 2, 1000, 100, cost_usd=0.0028, provider="openai")
            store.record_model_usage("ask", "", "", "llama-x", 1, 500, 50, cost_usd=None, provider="ollama")
            metrics.agent_run("sre", "ok", 12.5)
            metrics.agent_run("sre", "failed", 0.2)
            body = (await cx.get("/metrics")).text
            ck("model calls and tokens per provider and surface",
               sample(body, "tares_model_calls_total", provider="openai", surface="agent") == 2.0
               and sample(body, "tares_model_tokens_total", provider="openai", surface="agent", direction="input") == 1000.0
               and sample(body, "tares_model_tokens_total", provider="ollama", surface="ask", direction="output") == 50.0)
            ck("priced spend added, unpriced counted apart",
               abs((sample(body, "tares_model_spend_usd_total", provider="openai", surface="agent") or 0) - 0.0028) < 1e-9
               and sample(body, "tares_model_unpriced_calls_total", provider="ollama") == 1.0
               and sample(body, "tares_model_spend_usd_total", provider="ollama", surface="ask") is None)
            ck("agent runs per outcome, with a duration histogram",
               sample(body, "tares_agent_runs_total", agent="sre", status="ok") == 1.0
               and sample(body, "tares_agent_runs_total", agent="sre", status="failed") == 1.0
               and sample(body, "tares_agent_run_seconds_count", agent="sre") == 2.0)

            print("== event loop lag ==")
            await asyncio.sleep(1.3)
            body = (await cx.get("/metrics")).text
            lag = sample(body, "tares_event_loop_lag_seconds")
            ck("the loop watcher reports a lag figure", lag is not None and lag < 0.5, str(lag))
            import time
            time.sleep(1.2)   # block the loop on purpose, longer than the watcher interval so its timer is inside the block
            await asyncio.sleep(1.2)
            body = (await cx.get("/metrics")).text
            ck("a blocked loop counts as a stall", (sample(body, "tares_event_loop_stalls_total") or 0) >= 1, str(sample(body, "tares_event_loop_stalls_total")))

            print("== nothing sensitive in the body ==")
            ck("no entity keys or payload text", "checkout" not in body and "e0" not in body.split("tares_")[0])

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
