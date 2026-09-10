"""Instance metering — GET /api/usage. Real daemon: file sizes match the db on disk, per-source
counts agree with /api/sources, the TARES_MAX_DB_SIZE denominator drives max_bytes/pct_used
(null when unset), the endpoint is a read scope (ingest-only keys are refused), and the response
stays fast as the event count grows (no table scan).
"""
import asyncio, os, subprocess, sys, time
import httpx

from tares.daemon import _parse_size

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

SEED = "/tmp/usage_catalog.yaml"
with open(SEED, "w") as fh:
    fh.write("""sources:
  - name: evt
    connector: webhook
    poll: 5s
    config: {}
  - name: evt2
    connector: webhook
    poll: 5s
    config: {}
""")
DB, PORT = "/tmp/usage.duckdb", "8809"
AUTH = "root-token"
BLOCK = 1 << 18   # DuckDB writes in 256KiB blocks; sizes are compared to that tolerance


async def _wait(url):
    for _ in range(80):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def _boot(**extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("TARES_AUTH_TOKEN", "TARES_MAX_DB_SIZE")}
    env |= {"TARES_DB": DB, "TARES_CATALOG": SEED, "TARES_PORT": PORT,
            "TARES_OTLP_GRPC_PORT": "off", **extra}
    return subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _units():
    # TARES_MAX_DB_SIZE parsing: plain bytes and Kubernetes-style quantities (so Helm can pass
    # the PVC size through verbatim); anything unparseable degrades to "no limit".
    ck("size: plain bytes", _parse_size("1073741824") == 1 << 30)
    ck("size: 10Gi", _parse_size("10Gi") == 10 * (1 << 30))
    ck("size: 500Mi", _parse_size("500Mi") == 500 * (1 << 20))
    ck("size: 2G is decimal", _parse_size("2G") == 2 * 10**9)
    ck("size: unset -> None", _parse_size(None) is None and _parse_size("") is None)
    ck("size: garbage -> None", _parse_size("lots") is None and _parse_size("0") is None)


async def main():
    _units()
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    B = f"http://127.0.0.1:{PORT}"

    # ── no limit configured, auth off ──
    proc = _boot()
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient() as cx:
            await cx.post(f"{B}/ingest/evt", json=[{"m": i} for i in range(20)])
            await cx.post(f"{B}/ingest/evt2", json=[{"m": i} for i in range(5)])
            u = (await cx.get(f"{B}/api/usage")).json()

            db, wal = os.path.getsize(DB), (os.path.getsize(DB + ".wal") if os.path.exists(DB + ".wal") else 0)
            ck("db_bytes matches the file on disk", abs(u["db_bytes"] - db) <= BLOCK, f"{u['db_bytes']} vs {db}")
            ck("wal_bytes matches the wal on disk", abs(u["wal_bytes"] - wal) <= BLOCK, f"{u['wal_bytes']} vs {wal}")
            ck("db_bytes > 0", u["db_bytes"] > 0, str(u["db_bytes"]))
            ck("disk_total/disk_free are real",
               u["disk_total"] > 0 and 0 < u["disk_free"] <= u["disk_total"], str(u))
            ck("no TARES_MAX_DB_SIZE -> the volume is the limit",
               u["max_bytes"] == u["disk_total"] and u["max_bytes_source"] == "volume", str(u))
            ck("no TARES_MAX_DB_SIZE -> pct_used against the volume",
               u["pct_used"] is not None and 0 <= u["pct_used"] < 100, str(u["pct_used"]))
            ck("ingest not paused on a roomy volume", u["ingest_paused"] is False, str(u))
            ck("events total", u["events"] == 25, str(u["events"]))
            ck("per-source counts", {s["name"]: s["events"] for s in u["sources"]} == {"evt": 20, "evt2": 5},
               str(u["sources"]))
            ck("sources sum to events", sum(s["events"] for s in u["sources"]) == u["events"])
            ck("agent_runs / dispatch_deliveries present",
               u["agent_runs"] == 0 and u["dispatch_deliveries"] == 0, str(u))

            srcs = (await cx.get(f"{B}/api/sources")).json()
            from_sources = {s["name"]: (s["health"] or {}).get("events_total") for s in srcs}
            ck("per-source counts agree with /api/sources",
               all(from_sources.get(s["name"]) == s["events"] for s in u["sources"]), str(from_sources))

            # ── cost must not grow with the event count (counter read, not a scan) ──
            t0 = time.perf_counter(); await cx.get(f"{B}/api/usage"); small = time.perf_counter() - t0
            for _ in range(5):
                await cx.post(f"{B}/ingest/evt", json=[{"m": i, "text": "x" * 200} for i in range(2000)])
            t0 = time.perf_counter(); u2 = (await cx.get(f"{B}/api/usage")).json()
            big = time.perf_counter() - t0
            ck("counted the new events", u2["events"] == 10025, str(u2["events"]))
            ck("response time flat at 10k events (no scan)", big < max(0.25, small * 5),
               f"{small*1000:.1f}ms at 25 events -> {big*1000:.1f}ms at 10k")
    finally:
        proc.terminate(); proc.wait(timeout=10)

    # ── a limit the data already exceeds: ingest refused, polls paused, health says why ──
    proc = _boot(TARES_MAX_DB_SIZE="1Mi")
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (tiny limit)", False); return
        async with httpx.AsyncClient() as cx:
            h = (await cx.get(f"{B}/health")).json()
            ck("health degraded with the pause reason", h["status"] == "degraded"
               and "ingest paused" in (h.get("detail") or ""), str(h))
            u = (await cx.get(f"{B}/api/usage")).json()
            ck("usage reports ingest paused", u["ingest_paused"] is True and u["max_bytes_source"] == "env", str(u))
            r = await cx.post(f"{B}/ingest/evt", json=[{"m": "one more"}])
            ck("push ingest refused with 507", r.status_code == 507 and "ingest paused" in r.text, f"{r.status_code} {r.text[:120]}")
            r = await cx.post(f"{B}/v1/logs", json={"resourceLogs": []}, headers={"x-tares-source": "evt"})
            ck("OTLP ingest refused with 507 too", r.status_code == 507, str(r.status_code))
            ck("reads still work", (await cx.get(f"{B}/api/sources")).status_code == 200)
            u2 = (await cx.get(f"{B}/api/usage")).json()
            ck("nothing was stored while paused", u2["events"] == 10025, str(u2["events"]))
    finally:
        proc.terminate(); proc.wait(timeout=10)

    # ── limit configured + auth on; a limit with room lets ingest resume ──
    proc = _boot(TARES_AUTH_TOKEN=AUTH, TARES_MAX_DB_SIZE="1Gi")
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (auth)", False); return
        async with httpx.AsyncClient() as cx:
            ck("no credential -> 401", (await cx.get(f"{B}/api/usage")).status_code == 401)
            r = await cx.post(f"{B}/api/keys", json={"name": "meter", "scopes": ["read"]}, headers=H(AUTH))
            read_key = r.json()["secret"]
            ing_key = (await cx.post(f"{B}/api/keys", json={"name": "prod", "scopes": ["ingest"]},
                                     headers=H(AUTH))).json()["secret"]
            ck("ingest-only key -> 403", (await cx.get(f"{B}/api/usage", headers=H(ing_key))).status_code == 403)
            r = await cx.get(f"{B}/api/usage", headers=H(read_key))
            ck("read key -> 200", r.status_code == 200, r.text)
            u = r.json()
            ck("max_bytes from TARES_MAX_DB_SIZE", u["max_bytes"] == 1 << 30, str(u["max_bytes"]))
            expect = round(100 * (u["db_bytes"] + u["wal_bytes"]) / (1 << 30), 2)
            ck("pct_used = (db+wal)/max as a percentage", u["pct_used"] == expect and u["pct_used"] > 0,
               f"{u['pct_used']} vs {expect}")
            ck("counts survived the restart", u["events"] == 10025, str(u["events"]))
            ck("ingest resumes under a roomier limit", u["ingest_paused"] is False
               and (await cx.post(f"{B}/ingest/evt", json=[{"m": "after"}], headers=H(ing_key))).status_code == 202)
    finally:
        proc.terminate(); proc.wait(timeout=10)

    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
