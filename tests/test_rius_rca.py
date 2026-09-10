"""The rius_rca template (TR-223): the whole loop shape, minus the real Rius endpoints.

Run: .venv/bin/python tests/test_rius_rca.py   (no external services needed)

Asserts the five traps from the live hand-build (TR-223 comment) stay fixed: the ingest key is
stamped by the source's primary label (the service), the agent is born enabled with max_rounds 10 and the
callback configured, the text template carries the query identifiers, and the prompt permits
live MCP access.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = "/tmp/tares-rius-rca-test.duckdb"
CATALOG = "/tmp/tares-rius-rca-test.catalog.yaml"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = CATALOG

import httpx

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


PARAMS = {
    "mcp_url": "https://mcp.rius.invalid/mcp",
    "mcp_token": "gf_test_token",
    "callback_url": "https://ingest.rius.invalid/v1/rca/reports",
    "callback_token": "cb_test_token",
    "budget_usd": 5,
}


async def main():
    for p in (DB, DB + ".wal", CATALOG):
        if os.path.exists(p):
            os.remove(p)

    from tares.daemon import make_app
    app = make_app()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as cx:

            print("== template registration ==")
            r = await cx.get("/api/projects/templates")
            keys = [x["key"] for x in r.json()["templates"]]
            check("hidden: not in the gallery", "rius_rca" not in keys, str(keys))
            r = await cx.get("/api/projects/templates/rius_rca")
            check("hidden: still served by key (the Edit page needs its form)",
                  r.status_code == 200 and r.json()["key"] == "rius_rca"
                  and r.json()["params"]["mcp_token"].get("secret") is True, r.text[:200])
            r = await cx.get("/api/projects/templates/nope")
            check("unknown template by key -> 404", r.status_code == 404, r.text)

            print("== validation ==")
            r = await cx.post("/api/projects", json={"template": "rius_rca", "name": "r1",
                                                     "params": {}})
            check("missing params -> 400", r.status_code == 400, r.text)
            bad = dict(PARAMS, mcp_url="not-a-url")
            r = await cx.post("/api/projects", json={"template": "rius_rca", "name": "r1",
                                                     "params": bad})
            check("bad mcp_url -> 400", r.status_code == 400, r.text)
            bad = dict(PARAMS, budget_usd=-1)
            r = await cx.post("/api/projects", json={"template": "rius_rca", "name": "r1",
                                                     "params": bad})
            check("negative budget -> 400", r.status_code == 400, r.text)

            print("== create ==")
            r = await cx.post("/api/projects", json={"template": "rius_rca",
                                                     "name": "rca acme", "params": PARAMS})
            check("create -> 201", r.status_code == 201, r.text[:300])
            inst = r.json(); uid = inst["id"]
            kinds = sorted(o["kind"] for o in inst["objects"])
            check("five objects", kinds == ["agent", "mcp_server", "source", "trigger", "view"],
                  str(kinds))

            print("== the five traps stay fixed ==")
            srcs = {s["name"]: s for s in (await cx.get("/api/sources")).json()}
            src = srcs.get("rius_alerts") or {}
            labels = {l["name"]: l for l in (src.get("config") or {}).get("labels", [])}
            check("the service is the entity (primary label)",
                  labels.get("service", {}).get("primary") is True
                  and labels.get("delivery_id", {}).get("primary") is not True, str(labels))
            check("rule and delivery id are labels", "rule" in labels and "delivery_id" in labels, str(labels))
            views = {v["name"]: v for v in (await cx.get("/api/views")).json()}
            check("view keyed by service", views.get("rius_alerts_view", {}).get("key_field") == "service",
                  str(views.get("rius_alerts_view")))
            trig = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}.get("rius_alert_fired", {})
            check("one investigation per service per 5 minutes", trig.get("cooldown") == "5m", str(trig))
            tmpl = (src.get("config") or {}).get("text_template", "")
            check("text template carries the query identifiers",
                  all(k in tmpl for k in ("{delivery_id}", "{workspace_id}", "{service}",
                                          "{window_start}")), tmpl)
            ag = {a["name"]: a for a in
                  (await cx.get("/api/agents/builtin")).json()["agents"]}["rius_rca_agent"]
            check("agent enabled", ag["enabled"] is True)
            check("max_rounds 10", ag["max_rounds"] == 10, str(ag.get("max_rounds")))
            check("budget passed through", ag["budget_usd"] == 5.0, str(ag.get("budget_usd")))
            check("callback wired", ag["webhook_url"] == PARAMS["callback_url"]
                  and ag["webhook_token_configured"] is True, str(ag.get("webhook_url")))
            check("mcp server attached", ag["mcp_servers"] == ["rius"], str(ag.get("mcp_servers")))
            check("callback reports the delivery id as key", ag.get("webhook_key_label") == "delivery_id", str(ag.get("webhook_key_label")))
            check("prompt allows live access", "live access" in ag["prompt"]
                  and "no live access" not in ag["prompt"])
            mcp = {m["name"]: m for m in
                   (await cx.get("/api/mcp-servers")).json()["servers"]}["rius"]
            check("mcp auth configured, not echoed",
                  mcp["auth_value_configured"] is True and not mcp.get("auth_value"),
                  str(mcp)[:200])

            print("== ingest stamps the service as the key ==")
            body = {"delivery_id": "dlv-test-001", "alert_id": "al-1", "workspace_id": "ws-1",
                    "service": "canary-raw", "rule": "error rate > 25%",
                    "summary": "error rate 48% over 24h",
                    "window_start": "2026-09-02T00:00:00Z", "window_end": "2026-09-02T01:00:00Z"}
            r = await cx.post(f"/ingest/{src['ingest_key']}", json=body)
            check("alert ingests -> 202", r.status_code == 202, r.text)
            ev = (await cx.get("/api/sources/rius_alerts/events", params={"limit": 1})).json()
            check("stored key is the service", ev and ev[0]["key"] == "canary-raw", str(ev)[:200])
            ents = (await cx.get("/api/entities", params={"label": "rule"})).json()
            check("rule is a label on the event", "error rate > 25%" in json.dumps(ents), str(ents)[:200])
            ents = (await cx.get("/api/entities", params={"label": "delivery_id"})).json()
            check("delivery id is a label on the event", "dlv-test-001" in json.dumps(ents), str(ents)[:200])
            check("rendered line carries identifiers", ev and "dlv-test-001" in ev[0]["text"]
                  and "canary-raw" in ev[0]["text"], str(ev and ev[0]["text"]))

            print("== summary wiring panel ==")
            s = (await cx.get(f"/api/projects/{uid}/summary")).json()
            rows = {row["label"]: row["value"] for p_ in s.get("panels", [])
                    for row in p_["rows"]}
            check("panel shows ingest path and endpoints",
                  str(rows.get("alerts land at", "")).startswith("/ingest/")
                  and rows.get("agent reads") == PARAMS["mcp_url"]
                  and rows.get("reports go to") == PARAMS["callback_url"], str(rows))

            print("== delete releases nothing weird ==")
            r = await cx.delete(f"/api/projects/{uid}", params={"purge": "true"})
            check("delete -> 200", r.status_code == 200, r.text[:200])
            names = {s["name"] for s in (await cx.get("/api/sources")).json()}
            check("source gone", "rius_alerts" not in names, str(names))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
