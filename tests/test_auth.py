"""TARES_AUTH_TOKEN — self-hosted single-tenant auth. The API + console require the token; the
SPA shell, /health, and ingest (own token) stay public.
"""
import asyncio, os, signal, subprocess, sys
import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

SEED = "/tmp/auth_catalog.yaml"
with open(SEED, "w") as fh:
    fh.write("sources:\n  - name: evt\n    connector: webhook\n    poll: 5s\n    config: {}\n")
DB, PORT, TOKEN = "/tmp/auth.duckdb", "8804", "sekret-123"
LOGIN = "https://app.tares-glassflow.com/login"   # cloud login handoff (TARES_LOGIN_URL)


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
    env = {**os.environ, "TARES_DB": DB, "TARES_CATALOG": SEED, "TARES_PORT": PORT,
           "TARES_OTLP_GRPC_PORT": "off", "TARES_AUTH_TOKEN": TOKEN, "TARES_LOGIN_URL": LOGIN}
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{PORT}"
    auth = {"Authorization": f"Bearer {TOKEN}"}
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient() as cx:
            h = (await cx.get(f"{B}/health")).json()
            ck("/health public + advertises auth_required", h.get("auth_required") is True, str(h))
            ck("/health does not leak source names", h.get("sources") == [], str(h.get("sources")))
            ck("/health advertises login_url when TARES_LOGIN_URL set", h.get("login_url") == LOGIN, str(h))
            ck("console SPA shell public (GET /)", (await cx.get(f"{B}/")).status_code != 401)
            ck("static assets public (GET /assets/x)", (await cx.get(f"{B}/assets/whatever.js")).status_code != 401)

            ck("API without token -> 401", (await cx.get(f"{B}/api/connectors")).status_code == 401)
            ck("API with bearer token -> 200", (await cx.get(f"{B}/api/connectors", headers=auth)).status_code == 200)
            ck("API with X-Tares-Token -> 200", (await cx.get(f"{B}/api/sources", headers={"X-Tares-Token": TOKEN})).status_code == 200)
            ck("API with wrong token -> 401", (await cx.get(f"{B}/api/connectors", headers={"Authorization": "Bearer nope"})).status_code == 401)

            ck("POST /read without token -> 401", (await cx.post(f"{B}/read", json={"selector": {"a": "b"}})).status_code == 401)
            ck("POST /read with token -> not 401", (await cx.post(f"{B}/read", json={"selector": {"a": "b"}}, headers=auth)).status_code != 401)
            ck("catalog export protected", (await cx.get(f"{B}/api/catalog/export")).status_code == 401)

            # a secured instance gates ingest too: with any root token configured, anonymous
            # events would poison the timelines agents trust. The auth token itself ingests.
            ig = await cx.post(f"{B}/ingest/evt", json={"app": "a", "msg": "hi"})
            ck("anonymous ingest denied when auth is on (401)", ig.status_code == 401, str(ig.status_code))
            ig2 = await cx.post(f"{B}/ingest/evt", json={"app": "a", "msg": "hi"}, headers=auth)
            ck("auth token ingests (202)", ig2.status_code == 202, str(ig2.status_code))
    finally:
        proc.send_signal(signal.SIGTERM)
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
    print(f"\n{P} passed, {F} failed")

asyncio.run(main())
sys.exit(1 if F else 0)
