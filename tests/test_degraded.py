"""Degraded mode — a DuckDB that won't open must not cost the user the console.

Two real failures (an unreadable db file, and a second daemon on a db another daemon holds open):
the daemon must still bind, still serve the SPA, answer /health with a non-ok status that says why,
and 503 every data route instead of exiting before uvicorn binds (which the browser sees as
ERR_CONNECTION_REFUSED — a blank page with nothing to explain it). Plus the other half of the same
bug: /health must actually touch the store, so a wedged db can't read as healthy to a k8s probe.
"""
import asyncio, os, subprocess, sys
import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

SEED = "/tmp/degraded_catalog.yaml"
with open(SEED, "w") as fh:
    fh.write("sources:\n  - name: evt\n    connector: webhook\n    poll: 5s\n    config: {}\n")
OK_DB, BAD_DB, LOCK_DB = "/tmp/degraded_ok.duckdb", "/tmp/degraded_bad.duckdb", "/tmp/degraded_lock.duckdb"
PORT, PORT2 = "8811", "8812"


def _boot(db, port, **extra):
    env = {k: v for k, v in os.environ.items()
           if k not in ("TARES_AUTH_TOKEN", "TARES_MAX_DB_SIZE")}
    env |= {"TARES_DB": db, "TARES_CATALOG": SEED, "TARES_PORT": port,
            "TARES_OTLP_GRPC_PORT": "off", **extra}
    return subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def _health(port, tries=80):
    """Wait for /health to answer at all (any status) and return the body — the point of degraded
    mode is that it answers even when the store is gone."""
    for _ in range(tries):
        try:
            async with httpx.AsyncClient() as cx:
                r = await cx.get(f"http://127.0.0.1:{port}/health", timeout=1)
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return None


def _rm(*paths):
    for p in paths:
        for f in (p, p + ".wal"):
            if os.path.exists(f):
                os.chmod(f, 0o644)
                os.remove(f)


async def main():
    _rm(OK_DB, BAD_DB, LOCK_DB)

    # ── 1. healthy: /health proves the store answers, and reports usage on the 0-100 scale ──
    proc = _boot(OK_DB, PORT)
    try:
        h = await _health(PORT)
        ck("healthy daemon answers /health", h is not None)
        ck("status is ok", h and h["status"] == "ok", h)
        # the keys AuthGate + the cloud login handoff + the control plane's uptime check depend on
        ck("keeps auth_required/sources", h and "auth_required" in h and "sources" in h, h)
        # no limit configured: the volume the database sits on is the denominator (TR-290)
        ck("pct_used is measured against the volume with no limit configured",
           h and h.get("pct_used") is not None and h["pct_used"] < 100, h)
    finally:
        proc.terminate(); proc.wait(timeout=10)

    # A limit smaller than the db it measures → degraded, and pct_used reads as a percentage
    # (0-100), not a fraction: a real db is thousands of bytes over a 1000-byte cap.
    proc = _boot(OK_DB, PORT, TARES_MAX_DB_SIZE="1000")
    try:
        h = await _health(PORT)
        ck("near-full instance reports degraded", h and h["status"] == "degraded", h)
        ck("degraded says why", h and "detail" in h, h)
        ck("pct_used is 0-100, not a fraction", h and (h.get("pct_used") or 0) > 1, h)
    finally:
        proc.terminate(); proc.wait(timeout=10)

    # ── 2. hard failure: the db file exists but cannot be opened ──
    from tares.store import Store
    Store(BAD_DB).con.close()                # a real, valid db file…
    ck("seeded a db file to break", os.path.exists(BAD_DB))
    os.chmod(BAD_DB, 0o000)

    proc = _boot(BAD_DB, PORT)
    try:
        h = await _health(PORT)
        ck("daemon STARTS with an unopenable db (no ERR_CONNECTION_REFUSED)", h is not None)
        ck("status is not ok", h and h["status"] != "ok", h)
        ck("names the problem", h and BAD_DB in (h.get("detail") or ""), h)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}") as cx:
            r = await cx.get("/api/views", timeout=5)
            ck("/api/views is 503, not a bare 500", r.status_code == 503, r.status_code)
            ck("…with a reason the console can render",
               "unavailable" in (r.json().get("detail") or ""), r.text)
            r = await cx.post("/query", json={"view": "x", "key": "y"}, timeout=5)
            ck("/query is 503", r.status_code == 503, r.status_code)
            r = await cx.post("/ingest/evt", json={}, timeout=5)
            ck("ingest is 503 (a producer learns the write was refused)", r.status_code == 503,
               r.status_code)
            r = await cx.get("/", timeout=5)
            # the console is what shows the user the error, so it must still be served
            ck("the console SPA is still served", r.status_code == 200 or "console not built" in r.text,
               r.status_code)
    finally:
        proc.terminate(); proc.wait(timeout=10)
        os.chmod(BAD_DB, 0o644)

    # ── 3. lock contention: a second daemon on the same db degrades, the first is untouched ──
    first = _boot(LOCK_DB, PORT)
    second = None
    try:
        h = await _health(PORT)
        ck("first daemon is ok", h and h["status"] == "ok", h)
        second = _boot(LOCK_DB, PORT2)
        h2 = await _health(PORT2)
        ck("second daemon starts instead of crashing", h2 is not None)
        ck("second daemon is not ok", h2 and h2["status"] != "ok", h2)
        async with httpx.AsyncClient() as cx:
            r = await cx.get(f"http://127.0.0.1:{PORT2}/api/sources", timeout=5)
            ck("second daemon 503s its API", r.status_code == 503, r.status_code)
        h = await _health(PORT)
        ck("first daemon still ok", h and h["status"] == "ok", h)
    finally:
        for p in (first, second):
            if p:
                p.terminate(); p.wait(timeout=10)

    _rm(OK_DB, BAD_DB, LOCK_DB)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
