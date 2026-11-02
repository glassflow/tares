"""MCP tools per edition (TR-757): a factory cell lists its outcome tools first and hides the
building-block tools, an AI SRE cell keeps only `read` of them, a crew station sees everything,
a cell with no edition lists everything, and an unreachable daemon fails open to every tool.
The filter is a tidy list, not a permission boundary: the daemon still checks every call.
"""
import asyncio, json, os, signal, subprocess, sys, tempfile
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

DPORT, MPORT, TOKEN = "8856", "8857", "root-mcp"
URL = f"http://127.0.0.1:{MPORT}/mcp"
D = f"http://127.0.0.1:{DPORT}"
ROOT = {"Authorization": f"Bearer {TOKEN}"}
OBJECTS = ("read", "stats", "catalog_list", "catalog_describe", "list_connectors", "discover_source",
           "discover_docker", "test_source", "create_source", "list_sources", "source_fields",
           "create_trigger", "update_trigger", "subscribe")
OUTCOMES = ("what_needs_me", "how_is_project", "start_project", "hand_project_to_crew")


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


def _spawn(code, env):
    return subprocess.Popen([sys.executable, "-c", code], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(*procs):
    for p in procs:
        if p is None:
            continue
        p.send_signal(signal.SIGTERM)
        try: p.wait(timeout=5)
        except Exception: p.kill()


def _start(edition, daemon=True):
    tmp = tempfile.mkdtemp(prefix="mcp-editions-")
    base = clean_env(TARES_OTLP_GRPC_PORT="off", TARES_AUTH_TOKEN=TOKEN, TARES_EDITION=edition)
    d = None
    if daemon:
        d = _spawn("from tares.cli import run_daemon; run_daemon()",
                   {**base, "TARES_DB": os.path.join(tmp, "t.duckdb"), "TARES_CATALOG": "/tmp/none_me.yaml",
                    "TARES_PORT": DPORT})
    m = _spawn("from tares.cli import run_mcp; run_mcp()",
               {**base, "TARES_MCP_TRANSPORT": "streamable-http", "TARES_MCP_HOST": "127.0.0.1",
                "TARES_MCP_PORT": MPORT, "TARES_MCP_SURFACE_TTL": "0",
                "TARESD_URL": f"http://127.0.0.1:{DPORT}"})
    return d, m


async def _session(headers, fn):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


async def names(headers=ROOT):
    async def f(s):
        return [t.name for t in (await s.list_tools()).tools]
    return await _session(headers, f)


async def call(name, args, headers=ROOT):
    async def f(s):
        return (await s.call_tool(name, args)).content[0].text
    return await _session(headers, f)


async def up(daemon=True):
    if daemon and not await _wait(f"{D}/health"):
        return False
    return await _wait(URL, ok=lambda r: r.status_code < 500)


async def factory():
    print("== a factory cell ==")
    d, m = _start("factory")
    try:
        if not await up():
            ck("daemon and mcp up", False); return
        n = await names()
        ck("the outcome tools come first", n[:4] == list(OUTCOMES), str(n[:6]))
        ck("no building-block tool", not set(OBJECTS) & set(n), str(sorted(set(OBJECTS) & set(n))))
        ck("no answer_question", "answer_question" not in n)
        ck("the crew's tools stay", "list_tickets" in n and "desk_list" in n and "hand_over" in n, str(n))
        ck("a hidden tool answers in a sentence",
           await call("read", {"selector": {"a": "b"}}) == "This tool is off on this Tares. An admin "
           "can turn it on under Settings, Advanced.")
        ck("create_source is off too", "off on this Tares" in await call("create_source", {"name": "x"}))
        st = {"X-Tares-Role": "builder"}
        ck("a station sees the building blocks", "read" in await names({**ROOT, **st}))

        started = await call("start_project", {"name": "alpha", "goal": "ship the thing"})
        ck("start_project makes a project", '"name": "alpha"' in started, started)
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            r = await cx.post("/api/desk", json={"question": "Which colour for the links?",
                                                 "recommendation": "blue", "why": "it reads well",
                                                 "project": "alpha"})
            ck("a question on the desk", r.status_code == 201, r.text)
            w = (await cx.get("/api/whoami")).json()
            ck("whoami says object tools are off", w["mcp_object_tools"] is False, str(w))
            r = await cx.put("/api/settings/mcp", json={"object_tools": True},
                             headers={"Authorization": "Bearer nope"})
            ck("a bad key cannot change it", r.status_code in (401, 403), r.text)
            k = (await cx.post("/api/keys", json={"name": "r", "scopes": ["read"]})).json()
            r = await cx.put("/api/settings/mcp", json={"object_tools": True},
                             headers={"Authorization": f"Bearer {k['secret']}"})
            ck("a read key cannot change it", r.status_code == 403, r.text)
        waits = json.loads(await call("what_needs_me", {}))
        ck("what_needs_me returns the home items", isinstance(waits.get("waiting_for_you"), list)
           and "headline" in waits and any("colour" in json.dumps(i) for i in waits["waiting_for_you"]),
           str(waits)[:300])
        row = json.loads(await call("how_is_project", {"project": "alpha"}))
        ck("how_is_project returns the project's row", row.get("name") == "alpha" and "tickets" in row, str(row))
        miss = await call("how_is_project", {"project": "nope"})
        ck("an unknown project names the ones there are", miss == "No project called nope. Projects: alpha.", miss)
        sent = await call("hand_project_to_crew", {"project": "alpha", "repos": [
            {"path": "/tmp/alpha", "name": "acme/alpha"}]})
        ck("hand_project_to_crew records the hand-over", sent.startswith("Recorded") and "/tmp/alpha" in sent, sent)

        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            r = await cx.put("/api/settings/mcp", json={"object_tools": True})
            ck("an admin turns object tools on", r.status_code == 200 and r.json()["object_tools"] is True, r.text)
        n = await names()
        ck("the building blocks come back", set(OBJECTS) <= set(n), str(sorted(set(OBJECTS) - set(n))))
        ck("and the outcome tools still lead", n[:4] == list(OUTCOMES), str(n[:6]))
        ck("a hidden tool runs again", "off on this Tares" not in await call("list_connectors", {}))
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            r = await cx.put("/api/settings/mcp", json={"object_tools": None})
            ck("null clears the choice", r.status_code == 200 and r.json()["object_tools"] is False, r.text)
        ck("and they are hidden again", "read" not in await names())
    finally:
        _stop(m, d)


async def sre():
    print("== an AI SRE cell ==")
    d, m = _start("sre")
    try:
        if not await up():
            ck("daemon and mcp up", False); return
        n = await names()
        ck("read stays", "read" in n, str(n))
        ck("the other building blocks are hidden", not (set(OBJECTS) - {"read"}) & set(n),
           str(sorted((set(OBJECTS) - {"read"}) & set(n))))
        ck("no factory outcome tool", not set(OUTCOMES) & set(n), str(sorted(set(OUTCOMES) & set(n))))
        ck("the crew's tools stay", "list_tickets" in n and "desk_list" in n)
        ck("create_source answers in a sentence", "off on this Tares" in await call("create_source", {"name": "x"}))
    finally:
        _stop(m, d)


async def platform():
    print("== a cell with no edition ==")
    d, m = _start("")
    try:
        if not await up():
            ck("daemon and mcp up", False); return
        n = await names()
        ck("every tool is listed", set(OBJECTS) | set(OUTCOMES) <= set(n) and "list_tickets" in n,
           str(sorted((set(OBJECTS) | set(OUTCOMES)) - set(n))))
    finally:
        _stop(m, d)


async def unreachable():
    print("== the daemon is unreachable ==")
    d, m = _start("factory", daemon=False)
    try:
        if not await up(daemon=False):
            ck("mcp up", False); return
        n = await names()
        ck("it fails open to every tool", set(OBJECTS) | set(OUTCOMES) <= set(n), str(n))
    finally:
        _stop(m, d)


def words():
    print("== the outcome tools follow the words list ==")
    from tares import mcp_server, words as w
    tools = {t.name: t for t in mcp_server.mcp._tool_manager.list_tools()}
    ck("all four are registered", set(OUTCOMES) == set(mcp_server.FACTORY_TOOLS) <= set(tools))
    bad = [(n, w.offenders(n) + w.offenders(tools[n].description or "")) for n in mcp_server.FACTORY_TOOLS]
    ck("no forbidden word or em dash in a name or description", not any(b for _, b in bad), str(bad))


async def main():
    words()
    await factory()
    await sre()
    await platform()
    await unreachable()
    print(f"\n{P} passed, {F} failed")

asyncio.run(main())
sys.exit(1 if F else 0)
