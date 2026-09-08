"""Convergence test for the canonical connector schema: a source set up multiple ways
(API verbose, API minimal, YAML scrambled) must normalize to the identical stored config and
the identical exported YAML. Plus terse default-dropping, required enforcement, unknown-key
rejection. Self-contained — no external services.

Run: .venv/bin/python tests/test_normalize.py
"""
import asyncio
import os
import sys

os.environ["TARES_DB"] = "/tmp/tares-normalize-test.duckdb"
os.environ["TARES_CATALOG"] = "/tmp/does-not-exist.yaml"

import httpx
import yaml

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


async def main():
    for p in (os.environ["TARES_DB"], os.environ["TARES_DB"] + ".wal"):
        if os.path.exists(p):
            os.remove(p)

    from tares.connectors import normalize_config, full_schema, REGISTRY
    from tares.config import CatalogError
    from tares.daemon import make_app

    print("== every connector is schema-backed (canonical-config migration complete) ==")
    unmigrated = [n for n in REGISTRY if full_schema(n) is None]
    check("all connectors declare a CONFIG_SCHEMA", not unmigrated, f"missing: {unmigrated}")
    # a connector canonicalizes: terse defaults dropped, bool coerced, unknowns rejected
    a = normalize_config("prometheus_alerts",
                         {"url": "http://p:9090", "default_key": "unknown", "include_pending": "true"})
    check("prometheus_alerts: default_key==default dropped, bool kept",
          a == {"url": "http://p:9090", "include_pending": True}, str(a))
    try:
        normalize_config("docker_logs", {"container": "c", "bogus": 1})
        check("docker_logs rejects unknown key", False)
    except CatalogError:
        check("docker_logs rejects unknown key", True)

    print("== unit: normalize is terse + ordered ==")
    canon = normalize_config("prometheus", {
        "default_key": "unknown",                 # == default -> dropped
        "url": "http://p:9090",
        "queries": [
            {"promql": "m1", "event_type": "metric", "field": "value",      # all defaults
             "text": "{key} {field}={val}"},                                # -> just promql
            {"promql": "m2", "event_type": "five_xx", "key_label": "service"},
        ],
    })
    check("default_key dropped, url first", list(canon) == ["url", "queries"], str(list(canon)))
    check("fully-defaulted query collapses to promql", canon["queries"][0] == {"promql": "m1"},
          str(canon["queries"][0]))
    check("non-default query keeps its keys",
          canon["queries"][1] == {"promql": "m2", "event_type": "five_xx", "key_label": "service"},
          str(canon["queries"][1]))

    for bad, why in [
        ({"queries": [{"promql": "x"}]}, "missing required url"),
        ({"url": "http://p:9090"}, "missing required queries"),
        ({"url": "http://p:9090", "queries": [{"promql": "x"}], "bogus": 1}, "unknown top-level key"),
        ({"url": "http://p:9090", "queries": [{"promql": "x", "nope": 1}]}, "unknown key in a query"),
        ({"url": "http://p:9090", "queries": [{"event_type": "x"}]}, "query missing promql"),
    ]:
        try:
            normalize_config("prometheus", bad)
            check(f"reject: {why}", False)
        except CatalogError:
            check(f"reject: {why}", True)

    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

        print("== convergence: 3 ways -> identical stored config ==")
        # Way A — verbose: explicit defaults + empty optionals that should all drop
        await cx.post("/api/sources", json={"name": "a_verbose", "connector": "webhook", "config": {
            "key": "", "key_field": "service", "event_type": "reqs", "event_type_field": "",
            "text_template": "", "event_time_field": "",
            "labels": [{"name": "env", "const": "prod"}]}})
        # Way B — minimal: only the non-defaults
        await cx.post("/api/sources", json={"name": "b_minimal", "connector": "webhook", "config": {
            "key_field": "service", "event_type": "reqs",
            "labels": [{"name": "env", "const": "prod"}]}})
        # Way C — YAML import, keys in scrambled order + a redundant default
        ydoc = (
            "sources:\n"
            "  - name: c_yaml\n"
            "    connector: webhook\n"
            "    config:\n"
            "      labels:\n"
            "        - {name: env, const: prod}\n"
            "      event_type: reqs\n"
            "      event_type_field: ''\n"
            "      key_field: service\n")
        r = await cx.post("/api/catalog/import", json={"yaml": ydoc, "mode": "merge"})
        check("YAML import ok", r.status_code == 200, r.text)

        cfgs = {s["name"]: s["config"] for s in (await cx.get("/api/sources")).json()}
        target = {"key_field": "service", "event_type": "reqs", "labels": [{"name": "env", "const": "prod"}]}
        check("Way A == canonical", cfgs["a_verbose"] == target, str(cfgs["a_verbose"]))
        check("Way B == canonical", cfgs["b_minimal"] == target, str(cfgs["b_minimal"]))
        check("Way C (YAML) == canonical", cfgs["c_yaml"] == target, str(cfgs["c_yaml"]))
        check("all three byte-identical",
              cfgs["a_verbose"] == cfgs["b_minimal"] == cfgs["c_yaml"])
        # key order identical too (dict order is preserved through store + JSON)
        check("key order identical",
              list(cfgs["a_verbose"]) == list(cfgs["b_minimal"]) == list(cfgs["c_yaml"]),
              str(list(cfgs["a_verbose"])))

        print("== export YAML: the three source blocks are identical (minus name) ==")
        y = (await cx.get("/api/catalog/export")).text
        doc = yaml.safe_load(y)
        blocks = {s["name"]: {k: v for k, v in s.items() if k != "name"} for s in doc["sources"]}
        check("exported config blocks identical",
              blocks["a_verbose"] == blocks["b_minimal"] == blocks["c_yaml"], str(blocks["a_verbose"]))

        print("== round-trip stability: export -> import(replace) -> export is a fixed point ==")
        r = await cx.post("/api/catalog/import", json={"yaml": y, "mode": "replace"})
        check("re-import ok", r.status_code == 200, r.text)
        y2 = (await cx.get("/api/catalog/export")).text
        check("export is stable across a round-trip", y == y2,
              "first/second differ")

        print("== API rejects bad config (normalize enforces the schema) ==")
        r = await cx.post("/api/sources", json={"name": "bad", "connector": "prometheus",
                                                "config": {"url": "http://p:9090", "queries": [{"promql": "x"}], "junk": 1}})
        check("unknown config key -> 400", r.status_code == 400, r.text)
        r = await cx.post("/api/sources", json={"name": "bad2", "connector": "prometheus",
                                                "config": {"queries": [{"promql": "x"}]}})
        check("missing required url -> 400", r.status_code == 400, r.text)

        await cx.aclose()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())
