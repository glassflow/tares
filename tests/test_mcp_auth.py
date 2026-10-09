"""Remote MCP + TARES_AUTH_TOKEN — the MCP endpoint requires the bearer token from the agent, and
forwards it to taresd (whose API also requires it). Without the token, the connection is refused.
"""
import asyncio, os, signal, subprocess, sys
import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

DB, DPORT, MPORT, TOKEN = "/tmp/mcp_auth.duckdb", "8805", "8806", "tok-xyz"


async def _wait(url, ok=lambda r: r.status_code == 200):
    for _ in range(80):
        try:
            async with httpx.AsyncClient() as cx:
                if ok(await cx.get(url, timeout=1)):
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


async def main():
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    base = {**os.environ, "TARES_OTLP_GRPC_PORT": "off", "TARES_AUTH_TOKEN": TOKEN}
    daemon = subprocess.Popen(
        [sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
        env={**base, "TARES_DB": DB, "TARES_CATALOG": "/tmp/none_ma.yaml", "TARES_PORT": DPORT},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    mcpsrv = subprocess.Popen(
        [sys.executable, "-c", "from tares.cli import run_mcp; run_mcp()"],
        env={**base, "TARES_MCP_TRANSPORT": "streamable-http", "TARES_MCP_HOST": "127.0.0.1",
             "TARES_MCP_PORT": MPORT, "TARESD_URL": f"http://127.0.0.1:{DPORT}"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    URL = f"http://127.0.0.1:{MPORT}/mcp"
    try:
        if not await _wait(f"http://127.0.0.1:{DPORT}/health"):
            ck("daemon up", False); return
        if not await _wait(URL, ok=lambda r: r.status_code < 500):
            ck("mcp up", False); return

        # no token -> refused
        try:
            async with streamablehttp_client(URL) as (r, w, _):
                async with ClientSession(r, w) as s:
                    await s.initialize()
            ck("MCP without token refused", False, "initialize succeeded")
        except Exception:
            ck("MCP without token refused", True)

        # with token -> works, and tools proxy to taresd (which also requires the token)
        hdr = {"Authorization": f"Bearer {TOKEN}"}
        async with streamablehttp_client(URL, headers=hdr) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                ck("MCP with token: initialized", True)
                names = {t.name for t in (await s.list_tools()).tools}
                ck("tools listed with token", "read" in names and "list_connectors" in names, str(sorted(names)))
                res = await s.call_tool("list_connectors", {})
                ck("token forwarded to taresd (call succeeds)", "postgres" in res.content[0].text, res.content[0].text[:80])
    finally:
        for proc in (mcpsrv, daemon):
            proc.send_signal(signal.SIGTERM)
            try: proc.wait(timeout=5)
            except Exception: proc.kill()
    print(f"\n{P} passed, {F} failed")

asyncio.run(main())
sys.exit(1 if F else 0)
