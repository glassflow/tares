"""Remote MCP transport — a real MCP client connects over streamable-HTTP and calls tools that
proxy to a live taresd. Proves an agent can reach Tares without stdio (the demo/server path).
"""
import asyncio, os, signal, subprocess, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env, remove_db

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

DB = "/tmp/mcp_remote.duckdb"
DPORT, MPORT = "8801", "8802"


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
    remove_db(DB)
    base = {**clean_env(), "TARES_OTLP_GRPC_PORT": "off"}
    daemon = subprocess.Popen(
        [sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
        env={**base, "TARES_DB": DB, "TARES_CATALOG": "/tmp/none_mr.yaml", "TARES_PORT": DPORT},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    mcpsrv = subprocess.Popen(
        [sys.executable, "-c", "from tares.cli import run_mcp; run_mcp()"],
        env={**base, "TARES_MCP_TRANSPORT": "streamable-http", "TARES_MCP_HOST": "127.0.0.1",
             "TARES_MCP_PORT": MPORT, "TARESD_URL": f"http://127.0.0.1:{DPORT}"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not await _wait(f"http://127.0.0.1:{DPORT}/health"):
            ck("daemon up", False); return
        # MCP up = any HTTP response on /mcp (it 400/406s a bare GET, but that means it's listening)
        if not await _wait(f"http://127.0.0.1:{MPORT}/mcp", ok=lambda r: r.status_code < 500):
            ck("mcp server up", False); return

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(f"http://127.0.0.1:{MPORT}/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                ck("MCP client connected over HTTP + initialized", True)

                names = {t.name for t in (await session.list_tools()).tools}
                ck("read tools advertised over remote MCP", {"read", "catalog_list", "list_connectors"} <= names, str(sorted(names)))
                ck("write tools present (not read-only)", "create_source" in names, str(sorted(names)))

                res = await session.call_tool("list_connectors", {})
                text = res.content[0].text
                ck("call_tool proxies to taresd over remote transport", "postgres" in text and "webhook" in text, text[:80])

                cat = await session.call_tool("catalog_list", {})
                ck("catalog_list reachable remotely", cat.content[0].text.strip().startswith(("{", "[")), cat.content[0].text[:60])

        # Stateless: the server hands out no session id, and a request with no session works.
        # A kept session per client grew the cell's MCP sidecar until it was killed.
        hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "0"}}}
        async with httpx.AsyncClient() as cx:
            r = await cx.post(f"http://127.0.0.1:{MPORT}/mcp", json=init, headers=hdr)
            ck("initialize hands out no session id (stateless)",
               r.status_code == 200 and "mcp-session-id" not in r.headers, f"{r.status_code} {dict(r.headers)}")
            r = await cx.post(f"http://127.0.0.1:{MPORT}/mcp", headers=hdr,
                              json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            ck("tools/list works with no session", r.status_code == 200 and "list_connectors" in r.text, f"{r.status_code} {r.text[:80]}")
    finally:
        for proc in (mcpsrv, daemon):
            proc.send_signal(signal.SIGTERM)
            try: proc.wait(timeout=5)
            except Exception: proc.kill()

    print(f"\n{P} passed, {F} failed")

asyncio.run(main())
sys.exit(1 if F else 0)
