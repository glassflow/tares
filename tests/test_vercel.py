"""Vercel logs connector — map_payload mapping (deterministic) + ingest through /ingest/{source}."""
import asyncio, os
from _env import remove_db

os.environ["TARES_DB"] = "/tmp/vercel.duckdb"
os.environ["TARES_CATALOG"] = "/tmp/none.yaml"
remove_db("/tmp/vercel.duckdb")
import httpx
from tares.config import _source_from_dict
from tares.connectors import full_schema
from tares.connectors.vercel import VercelConnector

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

LABELS = [{"name": "project", "field": "project", "primary": True},
          {"name": "environment", "field": "environment"}, {"name": "source", "field": "source"}]
ENTRIES = [
    {"id": "1", "message": "Hello from API", "timestamp": 1700000000000, "type": "stdout",
     "source": "lambda", "projectName": "my-app", "deploymentId": "dpl_1",
     "environment": "production", "host": "my-app.vercel.app", "statusCode": 200},
    {"id": "2", "timestamp": 1700000001000, "source": "edge", "projectId": "prj_x",
     "environment": "preview", "proxy": {"method": "GET", "path": "/api/users", "statusCode": 500}},
]

# --- mapping (deterministic) ---
cfg = _source_from_dict({"name": "vercel", "connector": "vercel", "config": {"labels": LABELS}})
conn = VercelConnector(cfg, None)
envs = conn.map_payload(ENTRIES)
ck("two entries -> two envelopes", len(envs) == 2)
ck("lambda log: text=message, event_type=source", envs[0].text == "Hello from API" and envs[0].event_type == "lambda", envs[0].text)
ck("keyed by project (projectName)", envs[0].key_value == "my-app", envs[0].key_value)
ck("labels = project + environment + source", envs[0].labels == {"project": "my-app", "environment": "production", "source": "lambda"}, str(envs[0].labels))
ck("statusCode preserved in payload (not auto-fielded)", envs[0].payload.get("statusCode") == 200, str(envs[0].payload))
ck("request log: text synthesized from proxy", envs[1].text == "GET /api/users -> 500", envs[1].text)
ck("falls back to projectId when no projectName", envs[1].key_value == "prj_x", envs[1].key_value)
ck("proxy statusCode preserved in payload", (envs[1].payload.get("proxy") or {}).get("statusCode") == 500, str(envs[1].payload))
ck("full entry kept in payload", envs[0].payload.get("id") == "1")
ck("vercel is schema-backed", full_schema("vercel") is not None)

# --- ingest end to end through the daemon ---
from tares.daemon import make_app

def label_counts(app, source, label):
    """{value: lines} for one label of a source, over all time (the entity listing went, TR-1810)."""
    from datetime import datetime, timezone
    return app.state.store.aggregate([source], None, "count", datetime(2000, 1, 1, tzinfo=timezone.utc),
                                     group_by=label)


async def main():
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        await cx.post("/api/sources", json={"name": "vercel", "connector": "vercel", "config": {"labels": LABELS}})
        r = await cx.post("/ingest/vercel", json=ENTRIES)
        ck("POST /ingest/vercel -> 202", r.status_code == 202 and r.json()["ingested"] == 2, r.text)
        pv = label_counts(app, "vercel", "project")
        ck("per-project lines my-app + prj_x", pv == {"my-app": 1, "prj_x": 1}, str(pv))

        # editing a source is going-forward only (no inline backfill). `project` is synthesized at
        # ingest via label_context, so it stays on the already-stored events across an edit.
        r = await cx.put("/api/sources/vercel",
                         json={"name": "vercel", "connector": "vercel", "config": {"labels": LABELS}})
        ck("PUT source -> 200, no inline relabel", r.status_code == 200 and r.json().get("relabeled") is False, r.text)
        pv2 = label_counts(app, "vercel", "project")
        ck("synthesized project persists across an edit", pv2 == {"my-app": 1, "prj_x": 1}, str(pv2))

        # field profile: make the connector's normalized structure visible + honest about coverage.
        # ingest 3 more entries with NO project, so project is sparse (2 of 5) vs environment (5/5).
        await cx.post("/ingest/vercel", json=[
            {"source": "static", "environment": "production", "timestamp": 1700000002000},
            {"source": "static", "environment": "production", "timestamp": 1700000003000},
            {"source": "static", "environment": "production", "timestamp": 1700000004000}])
        prof = (await cx.get("/api/sources/vercel/fields")).json()
        fields = {f["name"]: f for f in prof["fields"]}
        ck("profile advertises provided fields (project primary)", "project" in fields and fields["project"]["primary_default"] is True, str(list(fields)))
        ck("project coverage is honest (2 of 5, not faked to 'unknown')", fields["project"]["coverage"] == 2, str(fields["project"]["coverage"]))
        ck("environment coverage is dense (5/5)", fields["environment"]["coverage"] == 5, str(fields["environment"]["coverage"]))
        ck("project values are the real names", {v["value"] for v in fields["project"]["values"]} == {"my-app", "prj_x"}, str(fields["project"]["values"]))
        await cx.aclose()
    print(f"\n{P} passed, {F} failed"); raise SystemExit(1 if F else 0)
asyncio.run(main())
