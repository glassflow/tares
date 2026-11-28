"""Degraded mode — a store that won't open must not cost the user the console.

Two real failures (a data directory the daemon cannot read, and a state database out of reach):
the daemon must still bind, still serve the SPA, answer /health with a non-ok status that says why,
and 503 every data route instead of exiting before uvicorn binds (which the browser sees as
ERR_CONNECTION_REFUSED — a blank page with nothing to explain it). Plus the other half of the same
bug: /health must actually touch the store, so a wedged db can't read as healthy to a k8s probe.
"""
import asyncio, os, subprocess, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env, remove_db

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

OK_DB, BAD_DB = "/tmp/degraded_ok", "/tmp/degraded_bad"
STATE_DB = "/tmp/degraded_state"
PORT, PORT2 = "8811", "8812"


def _boot(db, port, **extra):
    env = {k: v for k, v in clean_env().items()
           if k not in ("TARES_AUTH_TOKEN", "TARES_MAX_DB_SIZE")}
    env |= {"TARES_HOME": db, "TARES_PORT": port,
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
        if os.path.exists(p):
            os.chmod(p, 0o755)   # a run stopped half way leaves BAD_DB unreadable
    remove_db(*paths)


async def main():
    _rm(OK_DB, BAD_DB, STATE_DB)

    # ── 1. healthy: /health proves the store answers, and reports usage on the 0-100 scale ──
    proc = _boot(OK_DB, PORT)
    try:
        h = await _health(PORT)
        ck("healthy daemon answers /health", h is not None)
        ck("status is ok", h and h["status"] == "ok", h)
        # the keys AuthGate + the cloud login handoff + the control plane's uptime check depend on
        ck("keeps auth_required/sources", h and "auth_required" in h and "sources" in h, h)
        # what the crew CLI and the plugin read to refuse clearly when the cell is too old (TR-843)
        ck("lists the integrations, merge_queue, guardrail_rules, hook_points, guardrail_pairs, recall, launch_spec and github_token capabilities", h and h.get("capabilities") == ["integrations", "merge_queue", "guardrail_rules", "hook_points", "guardrail_pairs", "recall", "launch_spec", "github_token"], h)
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

    # ── 2. hard failure: the data directory holds a store but cannot be opened ──
    from tares.store import Store
    Store(BAD_DB, state_dsn="").close()      # a real, valid store…
    ck("seeded a store to break", os.path.exists(os.path.join(BAD_DB, "tares.sqlite")))
    os.chmod(BAD_DB, 0o000)

    # on SQLite: a cell on Postgres keeps nothing in its data directory to fail on
    proc = _boot(BAD_DB, PORT, TARES_TEST_PG="")
    try:
        h = await _health(PORT)
        ck("daemon STARTS with an unopenable db (no ERR_CONNECTION_REFUSED)", h is not None)
        ck("status is not ok", h and h["status"] != "ok", h)
        ck("a daemon that could not start lists no capabilities", h and h.get("capabilities") == [], h)
        ck("names the problem", h and BAD_DB in (h.get("detail") or ""), h)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}") as cx:
            r = await cx.get("/api/projects", timeout=5)
            ck("/api/projects is 503, not a bare 500", r.status_code == 503, r.status_code)
            ck("…with a reason the console can render",
               "unavailable" in (r.json().get("detail") or ""), r.text)
            r = await cx.post("/ingest/claude_code", json={}, timeout=5)
            ck("ingest is 503 (a producer learns the write was refused)", r.status_code == 503,
               r.status_code)
            r = await cx.get("/", timeout=5)
            # the console is what shows the user the error, so it must still be served
            ck("the console SPA is still served", r.status_code == 200 or "console not built" in r.text,
               r.status_code)
    finally:
        proc.terminate(); proc.wait(timeout=10)
        os.chmod(BAD_DB, 0o755)

    # ── 3. state database out of reach: degraded, and /health says so to the liveness probe ──
    # A cell (TARES_STATE_DSN) that starts while its Postgres cannot be reached would otherwise
    # stay degraded until restarted by hand; a 503 from /health gets the probe to restart it.
    try:
        import psycopg  # noqa: F401
    except ImportError:
        psycopg = None
    if psycopg is None:
        print("  skip state database out of reach (psycopg not installed)")
    elif os.getenv("TARES_TEST_PG"):
        # CI's Postgres run (tests/pg_harness) points every store at its own database, so the
        # unreachable DSN below would never be used; the plain run covers this case
        print("  skip state database out of reach (the Postgres harness replaces the DSN)")
    else:
        proc = _boot(STATE_DB, PORT, TARES_STATE_CONNECT_WAIT="0",
                     TARES_STATE_DSN="postgresql://u:p@127.0.0.1:1/db?connect_timeout=1")
        try:
            r = None
            for _ in range(80):
                try:
                    async with httpx.AsyncClient() as cx:
                        r = await cx.get(f"http://127.0.0.1:{PORT}/health", timeout=1)
                    break
                except Exception:
                    await asyncio.sleep(0.25)
            ck("daemon starts with its state database out of reach", r is not None)
            ck("/health answers 503, so the liveness probe restarts it",
               r is not None and r.status_code == 503, r and r.status_code)
            ck("…and still says why", r is not None and r.json().get("status") == "down"
               and "database" in (r.json().get("detail") or ""), r and r.text)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}") as cx:
                r = await cx.get("/api/projects", timeout=5)
                ck("its API is 503", r.status_code == 503, r.status_code)
        finally:
            proc.terminate(); proc.wait(timeout=10)

    _rm(OK_DB, BAD_DB, STATE_DB)
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
