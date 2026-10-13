"""One shared HTTP MCP server, many sessions (TR-469): each request's headers say who calls
(bearer, X-Tares-Station/Role/Parent, X-Tares-Project), so two concurrent sessions are each
attributed and gated by their own station, and an HTTP caller without a token never has the
server's token spent for it. stdio keeps reading the station from the environment.
"""
import asyncio, os, signal, socket, subprocess, sys, tempfile

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def free_port() -> str:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return str(port)


ROOT = "root-" + os.urandom(8).hex()
# identity must come from headers only: no station, project or token in the servers' environment
_STRIP = ("FACTORY_STATION", "FACTORY_ROLE", "FACTORY_PARENT", "TARES_PROJECT", "TARES_FACTORY_URL",
          "TARES_FACTORY_TOKEN_FILE", "TARES_AUTH_TOKEN", "TARESD_URL")


async def _wait(url, ok=lambda r: r.status_code == 200):
    for _ in range(120):
        try:
            async with httpx.AsyncClient() as cx:
                if ok(await cx.get(url, timeout=1)):
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


async def call(url, headers, tool, args):
    """One MCP session: initialize, call one tool, return its text."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
            return res.content[0].text if res.content else ""


def unit_checks():
    """In-process: the mode flag decides where identity comes from."""
    import tares.mcp_server as m
    keep = (m._HTTP, m.AUTH_TOKEN, {k: os.environ.get(k) for k in ("FACTORY_STATION", "FACTORY_ROLE")})
    try:
        os.environ["FACTORY_STATION"], os.environ["FACTORY_ROLE"] = "u-rev", "reviewer"
        m._HTTP = False
        ck("stdio: station headers come from the environment",
           m._station_headers() == {"X-Tares-Station": "u-rev", "X-Tares-Role": "reviewer"},
           str(m._station_headers()))
        m._HTTP = True
        ck("http: no request means no station, whatever the environment says",
           m._station_headers() == {}, str(m._station_headers()))
        m.AUTH_TOKEN = "srv"
        cx = m._cx()
        ck("http: a caller with no token is sent without the server's token",
           "authorization" not in cx.headers, str(dict(cx.headers)))
    finally:
        m._HTTP, m.AUTH_TOKEN = keep[0], keep[1]
        for k, v in keep[2].items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def main():
    tmp = tempfile.mkdtemp(prefix="mcp_station_headers_")
    base = {k: v for k, v in os.environ.items() if k not in _STRIP}
    base.update(TARES_OTLP_GRPC_PORT="off", HOME=tmp)
    dport, aport, bport = free_port(), free_port(), free_port()
    daemon_url = f"http://127.0.0.1:{dport}"
    procs = []

    def spawn(code, env, log):
        procs.append(subprocess.Popen([sys.executable, "-c", code], env={**base, **env},
                                      stdout=open(os.path.join(tmp, log), "w"),
                                      stderr=subprocess.STDOUT))

    spawn("from tares.cli import run_daemon; run_daemon()",
          {"TARES_DB": os.path.join(tmp, "tares.duckdb"), "TARES_CATALOG": os.path.join(tmp, "none.yaml"),
           "TARES_HOST": "127.0.0.1", "TARES_PORT": dport, "TARES_AUTH_TOKEN": ROOT}, "taresd.log")
    mcp_env = {"TARES_MCP_TRANSPORT": "streamable-http", "TARES_MCP_HOST": "127.0.0.1",
               "TARESD_URL": daemon_url}
    spawn("from tares.cli import run_mcp; run_mcp()",                   # A: gate on
          {**mcp_env, "TARES_MCP_PORT": aport, "TARES_AUTH_TOKEN": ROOT}, "mcp_a.log")
    spawn("from tares.cli import run_mcp; run_mcp()",                   # B: no token of its own
          {**mcp_env, "TARES_MCP_PORT": bport}, "mcp_b.log")
    a, b = f"http://127.0.0.1:{aport}/mcp", f"http://127.0.0.1:{bport}/mcp"
    auth = {"Authorization": f"Bearer {ROOT}"}
    try:
        if not await _wait(f"{daemon_url}/health"):
            ck("daemon up", False); return
        for url in (a, b):
            if not await _wait(url, ok=lambda r: r.status_code < 500):
                ck(f"mcp server up at {url}", False); return

        # a. two sessions at once on one server, each gated by its own station
        rev = {**auth, "X-Tares-Station": "t-rev", "X-Tares-Role": "reviewer"}
        orch = {**auth, "X-Tares-Station": "t-orch", "X-Tares-Role": "orchestrator"}
        desk = lambda q: {"question": q, "recommendation": "r", "why": "w"}
        r_rev, r_orch = await asyncio.gather(call(a, rev, "desk_add", desk("q-rev")),
                                             call(a, orch, "desk_add", desk("q-orch")))
        ck("reviewer station is refused desk_add", "only the orchestrator" in r_rev, r_rev[:200])
        ck("orchestrator station, at the same time, may desk_add", "q-orch" in r_orch, r_orch[:200])

        # c. a bearer with no station is not narrowed
        r_none = await call(a, auth, "desk_add", desk("q-none"))
        ck("no station headers: desk_add allowed", "q-none" in r_none, r_none[:200])

        # f. a server with no token of its own still forwards the caller's bearer and station
        r_b = await call(b, {**orch, "X-Tares-Station": "t-orch-b"}, "desk_add", desk("q-b"))
        ck("server without a token forwards the caller's bearer", "q-b" in r_b, r_b[:200])

        # b. Tares's own record
        async with httpx.AsyncClient(base_url=daemon_url, headers=auth, timeout=10) as cx:
            qs = [d["question"] for d in (await cx.get("/api/desk")).json()["desk"]]
            ck("the desk holds q-orch, not q-rev", "q-orch" in qs and "q-rev" not in qs, str(qs))

            # d. X-Tares-Project is the default project
            ids = []
            for name in ("hdr-one", "hdr-two"):
                r = await cx.post("/api/projects", json={"template": "custom", "name": name, "objects": []})
                ids.append(r.json().get("id"))
            r = await cx.post(f"/api/projects/{ids[0]}/docs",
                              json={"kind": "note", "title": "only-in-one", "body": "x"})
            ck("projects and a doc made", all(ids) and r.status_code == 201, r.text[:200])
        with_hdr = await call(a, {**auth, "X-Tares-Project": "hdr-one"}, "list_docs", {})
        ck("X-Tares-Project names the project a call means", "only-in-one" in with_hdr, with_hdr[:200])
        without = await call(a, auth, "list_docs", {})
        ck("without it the server asks which project", without.startswith("name the project"),
           without[:200])

        # e. the gate still holds on the server with a token
        async with httpx.AsyncClient() as cx:
            r = await cx.post(a, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                              headers={"Accept": "application/json, text/event-stream"})
        ck("no Authorization on a gated server: 401", r.status_code == 401, str(r.status_code))
        async with httpx.AsyncClient() as cx:
            r = await cx.post(a, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                              headers={"Accept": "application/json, text/event-stream",
                                       "Authorization": "Bearer"})
        ck("an empty bearer on a gated server: 401", r.status_code == 401, str(r.status_code))
    finally:
        for proc in procs:
            proc.send_signal(signal.SIGTERM)
            try: proc.wait(timeout=5)
            except Exception: proc.kill()

    unit_checks()   # g.
    print(f"\n{P} passed, {F} failed")

asyncio.run(main())
sys.exit(1 if F else 0)
