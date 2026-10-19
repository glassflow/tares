"""Generated ingest_key for push sources: /ingest/<key> (stable, unguessable) alongside the name,
NDJSON + empty-body handling, and the Vercel x-vercel-verify echo.
"""
import asyncio, os, signal, subprocess, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

DB, PORT = "/tmp/ingkey.duckdb", "8808"


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


async def main():
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    env = {**clean_env(), "TARES_DB": DB, "TARES_CATALOG": "/tmp/none_ik.yaml",
           "TARES_PORT": PORT, "TARES_OTLP_GRPC_PORT": "off"}
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{PORT}"
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient() as cx:
            cr = (await cx.post(f"{B}/api/sources", json={"name": "evt", "connector": "webhook", "config": {}})).json()
            ck("create response returns the ingest_key", bool(cr.get("ingest_key")), str(cr))
            src = (await cx.get(f"{B}/api/sources")).json()[0]
            key = src.get("ingest_key")
            ck("source gets a generated ingest_key", bool(key) and key.startswith("webhook-"), str(key))
            ck("create response key matches the source", cr.get("ingest_key") == key)

            ck("ingest by key -> 202", (await cx.post(f"{B}/ingest/{key}", json={"a": 1})).status_code == 202)
            ck("ingest by name still works", (await cx.post(f"{B}/ingest/evt", json={"a": 2})).status_code == 202)
            ck("unknown token -> 404", (await cx.post(f"{B}/ingest/webhook-deadbeef", json={"a": 1})).status_code == 404)

            r = await cx.post(f"{B}/ingest/{key}", content='{"a":3}\n{"a":4}\n', headers={"content-type": "application/x-ndjson"})
            ck("NDJSON body -> 2 ingested", r.status_code == 202 and r.json()["ingested"] == 2, r.text)
            r = await cx.post(f"{B}/ingest/{key}", content="")
            ck("empty body (verification ping) -> 202, 0 ingested", r.status_code == 202 and r.json()["ingested"] == 0, r.text)

            # key is stable across an update
            await cx.put(f"{B}/api/sources/evt", json={"name": "evt", "connector": "webhook", "config": {"event_type": "log"}})
            ck("ingest_key stable across update", (await cx.get(f"{B}/api/sources")).json()[0]["ingest_key"] == key)

            # vercel verify — the endpoint echoes the x-vercel-verify value Vercel sends on its probe
            # (how the drain flow verifies). No configured/env fallback: absent header → no echo.
            g = await cx.get(f"{B}/ingest/{key}")
            ck("GET probe with no verify header → none echoed", g.headers.get("x-vercel-verify") is None)
            g2 = await cx.get(f"{B}/ingest/{key}", headers={"x-vercel-verify": "echo-me"})
            ck("GET probe echoes the request's x-vercel-verify", g2.headers.get("x-vercel-verify") == "echo-me")
            p = await cx.post(f"{B}/ingest/{key}", json={"a": 9}, headers={"x-vercel-verify": "echo-me"})
            ck("POST response echoes the request's x-vercel-verify", p.headers.get("x-vercel-verify") == "echo-me")
    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
    print(f"\n{P} passed, {F} failed")

asyncio.run(main())
sys.exit(1 if F else 0)
