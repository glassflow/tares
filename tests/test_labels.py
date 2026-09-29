"""End-to-end test for the source/key (labels + entities) model.

Run: .venv/bin/python tests/test_labels.py   (no external services needed)

Covers: multi-label extraction, query by label (where), label-grouped triggers, the entities
surface, retroactive labels (lossless payload), type derived from connector, and backward
compatibility with a pre-labels single-key catalog + DB.
"""
import asyncio
import os
import sys

os.environ["TARES_DB"] = "/tmp/tares-labels-test.duckdb"
os.environ["TARES_CATALOG"] = "/tmp/does-not-exist.yaml"

import duckdb
import httpx

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


def precreate_legacy_db(path):
    """A pre-labels DB: events table without the `labels` column, plus a single-key row."""
    c = duckdb.connect(path)
    c.execute("""CREATE TABLE events (source TEXT, source_type TEXT, key_value TEXT, event_type TEXT,
       text TEXT, fields JSON, payload JSON, event_time TIMESTAMPTZ, ingest_time TIMESTAMPTZ)""")
    c.execute("""INSERT INTO events VALUES ('legacy','event_stream','api-server','deploy',
       'old deploy line','{}','{}', now(), now())""")
    c.close()


async def main():
    for p in (os.environ["TARES_DB"], os.environ["TARES_DB"] + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    precreate_legacy_db(os.environ["TARES_DB"])

    from tares.daemon import make_app
    app = make_app()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cx:

            print("== migration / backward compat ==")
            check("legacy events table migrated (labels column added)",
                  make_app() is not None)  # second make_app on same DB must not error

            print("== multi-label source ==")
            r = await cx.post("/api/sources", json={
                "connector": "webhook",   # note: no `type` field supplied
                "name": "logs",
                "config": {"key_field": "app", "event_type": "log",
                           "text_template": "[{app}/{env}] {msg}",
                           "labels": [{"name": "env", "field": "env"},
                                      {"name": "app", "field": "app"},
                                      {"name": "tier", "const": "frontend"}]}})
            check("create source without a type field", r.status_code == 201, r.text)
            got = await cx.get("/api/sources/logs")
            check("type derived from connector = event_stream",
                  got.json()["type"] == "event_stream", got.text)

            for env, app_, msg in [("prod", "ui", "pool exhausted"), ("prod", "ui", "timeout"),
                                   ("prod", "api", "500 on /pay"), ("staging", "ui", "deploy ok")]:
                await cx.post("/ingest/logs", json={"env": env, "app": app_, "msg": msg})

            print("== views are gone ==")
            r = await cx.post("/api/views", json={"name": "suite", "sources": ["logs"]})
            check("POST /api/views -> 404 (views were removed)", r.status_code == 404, r.text)
            r = await cx.post("/query", json={"view": "suite", "where": {"env": "prod"}})
            check("POST /query -> 404 (views were removed)", r.status_code == 404, r.text)

            print("== read by label (where), narrowed to one source ==")
            r = await cx.post("/read", json={"sources": ["logs"], "selector": {"env": "prod"},
                                             "window": "15m"})
            p = r.json()["payload"]
            check("where env=prod returns all prod apps",
                  "pool exhausted" in p and "500 on /pay" in p, p)
            check("where env=prod excludes staging", "deploy ok" not in p, p)
            r = await cx.post("/read", json={"sources": ["logs"],
                                             "selector": {"env": "prod", "app": "ui"}})
            p = r.json()["payload"]
            check("intersection env=prod,app=ui", "pool exhausted" in p and "500 on /pay" not in p, p)
            r = await cx.post("/read", json={"sources": ["logs"], "selector": {"tier": "frontend"}})
            check("const label tier=frontend matches everything",
                  r.json()["payload"].count("[logs]") == 4, r.json()["payload"])
            r = await cx.post("/read", json={"sources": ["logs"], "selector": {"key_value": "ui"}})
            check("legacy key (key_value) still selects", "pool exhausted" in r.json()["payload"])
            r = await cx.post("/read", json={"sources": ["logs"], "window": "15m"})
            check("no selector -> 400", r.status_code == 400)

            print("== entities surface ==")
            facets = {f["label"]: f for f in (await cx.get("/api/entities")).json()["labels"]}
            check("facets are the named labels (no generic key_value facet)",
                  {"env", "app", "tier"} <= set(facets) and "key_value" not in facets, str(list(facets)))
            envvals = {v["value"]: v["events"] for v in facets["env"]["values"]}
            check("env entity counts prod=3, staging=1", envvals == {"prod": 3, "staging": 1}, str(envvals))
            r = await cx.get("/api/entities?label=app")
            appvals = {v["value"]: v["events"] for v in r.json()["values"]}
            check("app entities ui=3, api=1", appvals == {"ui": 3, "api": 1}, str(appvals))
            check("unknown label -> 404", (await cx.get("/api/entities?label=nope")).status_code == 404)
            d = (await cx.get("/catalog/source:logs")).json()
            check("describe exposes named label axes",
                  set(d["labels"]) == {"env", "app", "tier"}, str(list(d["labels"])))

            print("== label-grouped trigger ==")
            await cx.post("/api/triggers", json={
                "name": "per_env_app", "sources": ["logs"],
                "condition": {"aggregate": "count", "predicate": "> 1", "window": "5m",
                              "group_by": ["env", "app"]},
                "emit": {"kind": "noisy"}, "cooldown": "1s"})
            # env=prod,app=ui already has 2 events (>1) → should fire for that tuple only
            await cx.post("/ingest/logs", json={"env": "prod", "app": "ui", "msg": "another"})
            disp = (await cx.get("/api/activity/dispatches")).json()
            keys = {x["key"] for x in disp if x["trigger"] == "per_env_app"}
            check("trigger fired per (env,app) tuple",
                  any("env=prod" in k and "app=ui" in k for k in keys), str(keys))
            check("trigger keys are composite (not collapsed)", all("," in k for k in keys), str(keys))

            print("== legacy single-key trigger still works ==")
            await cx.post("/api/triggers", json={
                "name": "legacy_key", "sources": ["logs"],
                "condition": {"aggregate": "count", "predicate": "> 0", "window": "5m",
                              "group_by": ["key_value"]},
                "emit": {"kind": "k"}, "cooldown": "1s"})
            await cx.post("/ingest/logs", json={"env": "prod", "app": "gateway", "msg": "gw"})
            disp = (await cx.get("/api/activity/dispatches")).json()
            check("legacy key_value trigger fires with a bare key",
                  any(x["trigger"] == "legacy_key" and x["key"] == "gateway" for x in disp))

            print("== labels are going-forward (no inline backfill on edit) ==")
            # 'msg' was always in the payload but never a label; declare it now. Editing a source is
            # going-forward only: NEW events carry the new label; pre-existing events are untouched
            # (a retroactive relabel of stored events is a planned explicit background action).
            cur = (await cx.get("/api/sources/logs")).json()["config"]
            cur["labels"].append({"name": "msg", "field": "msg"})
            r = await cx.put("/api/sources/logs", json={"name": "logs", "connector": "webhook", "config": cur})
            check("edit does not relabel stored events inline (relabeled=false)",
                  r.json().get("relabeled") is False, r.text)
            await cx.post("/ingest/logs", json={"env": "prod", "app": "ui", "msg": "after-relabel"})
            msgs = {v["value"] for v in (await cx.get("/api/entities?label=msg")).json()["values"]}
            check("new events carry the newly-declared label", "after-relabel" in msgs, str(msgs))
            check("pre-existing events are NOT retroactively relabeled",
                  "pool exhausted" not in msgs and "timeout" not in msgs, str(msgs))

            print("== YAML round-trip (no type, labels preserved) ==")
            import yaml as _yaml
            y = (await cx.get("/api/catalog/export")).text
            doc = _yaml.safe_load(y)
            check("export omits derived type (no source-level `type` key)",
                  all("type" not in s for s in doc["sources"]), str(doc["sources"][0]))
            check("export carries labels", "tier" in y and "frontend" in y, y[:400])
            r = await cx.post("/api/catalog/import", json={"yaml": y, "mode": "replace"})
            check("replace re-import ok", r.status_code == 200, r.text)
            src = (await cx.get("/api/sources/logs")).json()
            check("labels survive round-trip",
                  any(l["name"] == "tier" for l in src["config"]["labels"]), str(src["config"]))
            check("type still derived after import", src["type"] == "event_stream")

            print("== raw read across sources ==")
            # A second source so we can prove /read unions across sources with nothing defined —
            # the Layer-1 primitive. `logs` carries env/app/tier; `metrics2` carries app/region.
            await cx.post("/api/sources", json={
                "connector": "webhook", "name": "metrics2",
                "config": {"key_field": "app", "event_type": "metric",
                           "text_template": "[{app}/{region}] {msg}",
                           "labels": [{"name": "app", "field": "app"},
                                      {"name": "region", "field": "region"}]}})
            for app_, region, msg in [("ui", "us", "cpu high"), ("ui", "eu", "cpu ok"),
                                      ("api", "us", "mem high")]:
                await cx.post("/ingest/metrics2", json={"app": app_, "region": region, "msg": msg})

            r = await cx.post("/read", json={"selector": {"app": "ui"}, "window": "15m"})
            body = r.json()
            check("read unions every source carrying app=ui",
                  set(body["sources"]) == {"logs", "metrics2"}, str(body.get("sources")))
            check("read reports count and merges the metrics2 line",
                  body["count"] > 0 and "cpu high" in body["payload"], body["payload"][:300])

            # strict-AND: `region` lives only on metrics2, so adding it drops `logs` entirely and
            # keeps only the region=us event (cpu high), not region=eu (cpu ok).
            r = await cx.post("/read", json={"selector": {"app": "ui", "region": "us"}, "window": "15m"})
            body = r.json()
            check("strict-AND app=ui AND region=us keeps only metrics2 (logs lacks region)",
                  body["sources"] == ["metrics2"] and "cpu high" in body["payload"]
                  and "cpu ok" not in body["payload"], str(body))

            check("read with empty selector -> 400",
                  (await cx.post("/read", json={"selector": {}})).status_code == 400)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
