"""Instance metering, GET /api/usage. Real daemon: the sizes are the session store's (the SQLite file
and its WAL on disk, or the Postgres database) plus the archive, `events` is the lines the sessions
hold (no per source counts since the source catalog went, TR-1811), the TARES_MAX_DB_SIZE denominator
drives max_bytes/pct_used, the endpoint is a read scope (ingest-only keys are refused), the response
stays fast as the lines grow (no table scan), and transcript lines that fill the limit pause ingest
(TR-1815). The store is measured at most every `Store.SIZE_FOR` seconds, so a read that must see a
change waits that long first.
"""
import asyncio, json, os, subprocess, sys, time
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env, remove_db

from tares.daemon import _parse_size
import tares.store as _store

# the Postgres harness (tests/pg_harness) puts the daemon's store on Postgres: no file to compare with
ON_PG = _store.Store.__init__.__name__ == "_init"
FRESH = _store.SIZE_FOR + 0.5   # seconds after which the next read measures the store again

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

DB, PORT = "/tmp/usage", "8809"
AUTH = "root-token"
BLOCK = 1 << 18   # the file grows while the test reads it: sizes are compared to this tolerance


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
    env = {k: v for k, v in clean_env().items()
           if k not in ("TARES_AUTH_TOKEN", "TARES_MAX_DB_SIZE")}
    env |= {"TARES_HOME": DB, "TARES_PORT": PORT, **extra}
    return subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def lines(session, n, size=0):
    """`n` Claude Code lines of `session`, as the plugin posts them."""
    return "\n".join(json.dumps({
        "sessionId": session, "type": "assistant", "cwd": "/w", "uuid": f"{session}-{size}-{i}",
        "timestamp": "2026-11-27T10:00:00Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": f"m{i} " + "x" * size}]}})
        for i in range(n)) + "\n"


async def ingest(cx, B, body, headers=None, **kw):
    return await cx.post(f"{B}/ingest/claude_code", content=body,
                         headers={"content-type": "application/x-ndjson", **(headers or {})}, **kw)


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
    remove_db(DB)
    B = f"http://127.0.0.1:{PORT}"

    # ── no limit configured, auth off ──
    proc = _boot()
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient() as cx:
            await ingest(cx, B, lines("a", 20))
            await ingest(cx, B, lines("b", 5))
            await asyncio.sleep(FRESH)
            u = (await cx.get(f"{B}/api/usage")).json()

            f = os.path.join(DB, "tares.sqlite")
            if ON_PG:
                ck("db_bytes is the Postgres database's size, and there is no WAL of its own",
                   u["db_bytes"] > 0 and u["wal_bytes"] == 0, str(u))
            else:
                db, wal = os.path.getsize(f), (os.path.getsize(f + "-wal") if os.path.exists(f + "-wal") else 0)
                ck("db_bytes matches the file on disk", abs(u["db_bytes"] - db) <= BLOCK, f"{u['db_bytes']} vs {db}")
                ck("wal_bytes matches the wal on disk", abs(u["wal_bytes"] - wal) <= BLOCK, f"{u['wal_bytes']} vs {wal}")
            ck("archive_bytes is there, nothing archived yet", u["archive_bytes"] == 0, str(u))
            ck("db_bytes > 0", u["db_bytes"] > 0, str(u["db_bytes"]))
            ck("disk_total/disk_free are real",
               u["disk_total"] > 0 and 0 < u["disk_free"] <= u["disk_total"], str(u))
            ck("no TARES_MAX_DB_SIZE -> the volume is the limit",
               u["max_bytes"] == u["disk_total"] and u["max_bytes_source"] == "volume", str(u))
            ck("no TARES_MAX_DB_SIZE -> pct_used against the volume",
               u["pct_used"] is not None and 0 <= u["pct_used"] < 100, str(u["pct_used"]))
            ck("ingest not paused on a roomy volume", u["ingest_paused"] is False, str(u))
            ck("events counts the lines the sessions hold", u["events"] == 25, str(u["events"]))
            ck("no per source counts", u["sources"] == [], str(u["sources"]))
            ck("agent_runs / dispatch_deliveries present",
               u["agent_runs"] == 0 and u["dispatch_deliveries"] == 0, str(u))

            # ── cost must not grow with the event count (counter read, not a scan) ──
            await asyncio.sleep(FRESH)            # both reads measure the store afresh
            t0 = time.perf_counter(); await cx.get(f"{B}/api/usage"); small = time.perf_counter() - t0
            for k in range(5):
                await ingest(cx, B, lines(f"big{k}", 1000, size=300), timeout=120)
            await asyncio.sleep(FRESH)
            t0 = time.perf_counter(); u2 = (await cx.get(f"{B}/api/usage")).json()
            big = time.perf_counter() - t0
            ck("counted the new lines", u2["events"] == 5025, str(u2["events"]))
            ck("response time flat at 5k events (no scan)", big < max(0.25, small * 5),
               f"{small*1000:.1f}ms at 25 events -> {big*1000:.1f}ms at 5k")
    finally:
        proc.terminate(); proc.wait(timeout=10)

    # ── a limit the data already exceeds: ingest refused, health says why ──
    proc = _boot(TARES_MAX_DB_SIZE="100Ki")   # less than the store alone
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (tiny limit)", False); return
        async with httpx.AsyncClient() as cx:
            h = (await cx.get(f"{B}/health")).json()
            ck("health degraded with the pause reason", h["status"] == "degraded"
               and "ingest paused" in (h.get("detail") or ""), str(h))
            u = (await cx.get(f"{B}/api/usage")).json()
            ck("usage reports ingest paused", u["ingest_paused"] is True and u["max_bytes_source"] == "env", str(u))
            r = await ingest(cx, B, lines("late", 1))
            ck("push ingest refused with 507", r.status_code == 507 and "ingest paused" in r.text, f"{r.status_code} {r.text[:120]}")
            r = await cx.post(f"{B}/ingest/github_app-0000000000000000", json={})
            ck("a webhook delivery is refused with 507 too", r.status_code == 507, str(r.status_code))
            ck("reads still work", (await cx.get(f"{B}/api/projects")).status_code == 200)
            u2 = (await cx.get(f"{B}/api/usage")).json()
            await asyncio.sleep(FRESH)
            u2 = (await cx.get(f"{B}/api/usage")).json()
            ck("nothing was stored while paused", u2["events"] == 5025, str(u2["events"]))
    finally:
        proc.terminate(); proc.wait(timeout=10)

    # ── limit configured + auth on; a limit with room lets ingest resume ──
    roomy = (100 << 30) if ON_PG else (1 << 30)   # a shared Postgres database is far bigger than one cell
    proc = _boot(TARES_AUTH_TOKEN=AUTH, TARES_MAX_DB_SIZE=str(roomy))
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
            ck("max_bytes from TARES_MAX_DB_SIZE", u["max_bytes"] == roomy, str(u["max_bytes"]))
            expect = round(100 * (u["db_bytes"] + u["wal_bytes"] + u["archive_bytes"]) / roomy, 2)
            ck("pct_used = (db+wal+archive)/max as a percentage", u["pct_used"] == expect and u["pct_used"] > 0,
               f"{u['pct_used']} vs {expect}")
            ck("counts survived the restart", u["events"] == 5025, str(u["events"]))
            ck("ingest resumes under a roomier limit", u["ingest_paused"] is False
               and (await ingest(cx, B, lines("after", 1), headers=H(ing_key))).status_code == 202)
    finally:
        proc.terminate(); proc.wait(timeout=10)

    # ── transcript lines that fill the limit pause ingest (TR-1815) ──
    proc = _boot()
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (growth)", False); return
        async with httpx.AsyncClient() as cx:
            u = (await cx.get(f"{B}/api/usage")).json()
            used = u["db_bytes"] + u["wal_bytes"] + u["archive_bytes"]
    finally:
        proc.terminate(); proc.wait(timeout=10)
    grow = 512 * 1024                 # the pause mark is this many bytes away
    limit = int((used + grow) / 0.95)
    proc = _boot(TARES_MAX_DB_SIZE=str(limit))
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (near the limit)", False); return
        async with httpx.AsyncClient() as cx:
            u = (await cx.get(f"{B}/api/usage")).json()
            ck("near the limit, ingest is not paused yet", u["ingest_paused"] is False, str(u))
            for k in range(12):
                r = await ingest(cx, B, lines(f"grow{k}", 500, size=600), timeout=120)
                if r.status_code != 202:
                    break
                await asyncio.sleep(FRESH)
            u = (await cx.get(f"{B}/api/usage")).json()
            ck("the transcript lines filled it: paused", u["ingest_paused"] is True and u["pct_used"] >= 95,
               str(u))
            r = await ingest(cx, B, lines("over", 1))
            ck("and the next post is refused with 507", r.status_code == 507, f"{r.status_code} {r.text[:120]}")
            h = (await cx.get(f"{B}/health")).json()
            ck("health says ingest is paused", h["status"] == "degraded" and "ingest paused" in (h.get("detail") or ""),
               str(h))
    finally:
        proc.terminate(); proc.wait(timeout=10)

    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
