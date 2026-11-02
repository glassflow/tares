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
           "can turn on the building-block tools under Settings, Access and API keys, Agent tools.")
        ck("create_source is off too", "off on this Tares" in await call("create_source", {"name": "x"}))
        st = {"X-Tares-Role": "builder"}
        ck("a station sees the building blocks", "read" in await names({**ROOT, **st}))
        # The server keeps one tool cache for every caller: once a station has listed `read`, the
        # person's call to it must still get the sentence, not an output check error.
        ck("a hidden tool answers after a station listed it",
           "off on this Tares" in await call("read", {"selector": {"a": "b"}}))
        ck("a station can call a hidden tool", "off on this Tares" not in
           await call("list_connectors", {}, {**ROOT, **st}))

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
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            home = (await cx.get("/api/home")).json()
        waits = json.loads(await call("what_needs_me", {}))
        ck("what_needs_me keeps the home headline", waits["headline"] == home["headline"] and home["headline"],
           str(waits.get("headline")))
        ck("what_needs_me returns the home items", isinstance(waits.get("waiting_for_you"), list)
           and "headline" in waits and any("colour" in json.dumps(i) for i in waits["waiting_for_you"]),
           str(waits)[:300])
        row = json.loads(await call("how_is_project", {"project": "alpha"}))
        ck("how_is_project returns the project's row", row.get("name") == "alpha" and "tickets" in row, str(row))
        byid = json.loads(await call("how_is_project", {"project": row.get("id", "x")}))
        ck("how_is_project finds a project by id", byid.get("name") == "alpha" and row.get("id"), str(byid))
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
        ck("and a building block runs", "off on this Tares" not in await call("list_connectors", {}))
        ck("and the outcome tools still lead", n[:4] == list(OUTCOMES), str(n[:6]))
        ck("a hidden tool runs again", "off on this Tares" not in await call("list_connectors", {}))
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            r = await cx.put("/api/settings/mcp", json={"object_tools": None})
            ck("null clears the choice", r.status_code == 200 and r.json()["object_tools"] is False, r.text)
        ck("and they are hidden again", "read" not in await names())
        ck("a call after they were listed and turned off gets the sentence",
           "off on this Tares" in await call("read", {"selector": {"a": "b"}}))
        ck("a bad key fails open to every tool and the call says 401",
           set(OBJECTS) <= set(await names({"Authorization": "Bearer nope"})))
        ck("what_needs_me reports an error from the daemon",
           (await call("what_needs_me", {}, {"Authorization": "Bearer nope"})).startswith("error 401"))
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
        ck("an outcome tool says it is not part of this edition",
           "not part of the sre edition" in await call("what_needs_me", {}))
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            r = await cx.put("/api/settings/mcp", json={"object_tools": True})
        n = await names()
        ck("object tools on, the factory outcome tools stay hidden on sre",
           set(OBJECTS) <= set(n) and not set(OUTCOMES) & set(n), str(sorted(set(OUTCOMES) & set(n))))
    finally:
        _stop(m, d)


async def platform():
    print("== a cell with no edition ==")
    d, m = _start("")
    try:
        if not await up():
            ck("daemon and mcp up", False); return
        n = await names()
        want = set(OBJECTS) | {"start_project", "hand_project_to_crew"}
        ck("every tool but the two home readers is listed", want <= set(n) and "list_tickets" in n,
           str(sorted(want - set(n))))
        ck("the two tools that read the factory home stay off", not {"what_needs_me", "how_is_project"} & set(n))
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            async def whoami():
                return (await cx.get("/api/whoami")).json()["mcp_object_tools"]
            ck("platform has object tools by default", await whoami() is True)
            await cx.put("/api/settings/mcp", json={"object_tools": False})
            ck("an admin can turn them off", await whoami() is False and "read" not in await names())
            await cx.put("/api/settings/mcp", json={"object_tools": None})
            ck("null brings the default back", await whoami() is True)
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
        ck("a call runs rather than answering the off sentence",
           "off on this Tares" not in await call("create_source", {"name": "x"}))
    finally:
        _stop(m, d)


def words():
    print("== the outcome tools follow the words list ==")
    from tares import mcp_server, words as w
    tools = {t.name: t for t in mcp_server.mcp._tool_manager.list_tools()}
    ck("all four are registered", set(OUTCOMES) == set(mcp_server.FACTORY_TOOLS) <= set(tools))
    bad = [(n, w.offenders(n) + w.offenders(tools[n].description or "")) for n in mcp_server.FACTORY_TOOLS]
    ck("no forbidden word or em dash in a name or description", not any(b for _, b in bad), str(bad))


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)
    def json(self):
        return self._body


async def unit():
    print("== the surface cache and the settings order, in process ==")
    from tares import mcp_server as ms, edition as ed

    class Store:
        def __init__(self, v=""): self.v = v
        def get_setting(self, k): return self.v

    saved = os.environ.pop(ed.OBJECT_TOOLS_ENV, None)
    try:
        ck("platform defaults on", ed.object_tools(Store(), "platform") is True)
        ck("factory defaults off", ed.object_tools(Store(), "factory") is False)
        ck("a stored 0 beats the default", ed.object_tools(Store("0"), "platform") is False)
        ck("a stored 1 wins", ed.object_tools(Store("1"), "factory") is True)
        os.environ[ed.OBJECT_TOOLS_ENV] = "1"
        ck("the env turns them on when nothing is stored", ed.object_tools(Store(), "factory") is True)
        ck("a stored 0 beats the env", ed.object_tools(Store("0"), "factory") is False)
        os.environ[ed.OBJECT_TOOLS_ENV] = "0"
        ck("the env turns them off on platform", ed.object_tools(Store(), "platform") is False)
    finally:
        os.environ.pop(ed.OBJECT_TOOLS_ENV, None)
        if saved is not None:
            os.environ[ed.OBJECT_TOOLS_ENV] = saved

    calls = []
    answer = {"r": _Resp(200, {"edition": "factory", "mcp_object_tools": False})}

    class FakeCx:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            calls.append(url)
            return answer["r"]

    real = (ms._cx, ms.AUTH_TOKEN, ms._HTTP)
    ms._cx = lambda *a, **k: FakeCx()
    ms._HTTP = False
    old_ttl = os.environ.get("TARES_MCP_SURFACE_TTL")
    try:
        os.environ["TARES_MCP_SURFACE_TTL"] = "30"
        ms._SURFACES.clear(); ms.AUTH_TOKEN = "t1"
        a = await ms._surface(); b = await ms._surface()
        ck("a second look within the TTL reuses the answer", a == b == ("factory", False) and len(calls) == 1, str(calls))
        ms.AUTH_TOKEN = "t2"
        await ms._surface()
        ck("each token has its own answer", len(calls) == 2, str(calls))
        os.environ["TARES_MCP_SURFACE_TTL"] = "0"
        await ms._surface()
        ck("a TTL of 0 asks every time", len(calls) == 3, str(calls))
        os.environ["TARES_MCP_SURFACE_TTL"] = "junk"
        await ms._surface()
        ck("a TTL that is not a number falls back to 30 s", len(calls) == 3, str(calls))
        os.environ["TARES_MCP_SURFACE_TTL"] = "30"
        ms._SURFACES.clear()
        for i in range(260):
            ms.AUTH_TOKEN = f"k{i}"
            await ms._surface()
        ck("the cache is emptied past 256 tokens", len(ms._SURFACES) < 260, str(len(ms._SURFACES)))
        ms._SURFACES.clear(); ms.AUTH_TOKEN = "t9"
        answer["r"] = _Resp(401, {})
        ck("a 401 gives the unknown edition and every tool", await ms._surface() == ("", True))
        ms._SURFACES.clear()
        answer["r"] = _Resp(200, {"edition": "factory"})
        ck("a body with no mcp_object_tools fails open", await ms._surface() == ("", True))
        ck("an unknown edition shows the factory tools and the building blocks",
           ms._visible("what_needs_me", "", True, False) and ms._visible("read", "", True, False))
    finally:
        ms._cx, ms.AUTH_TOKEN, ms._HTTP = real
        ms._SURFACES.clear()
        if old_ttl is None:
            os.environ.pop("TARES_MCP_SURFACE_TTL", None)
        else:
            os.environ["TARES_MCP_SURFACE_TTL"] = old_ttl


async def main():
    words()
    await unit()
    await factory()
    await sre()
    await platform()
    await unreachable()
    print(f"\n{P} passed, {F} failed")

asyncio.run(main())
sys.exit(1 if F else 0)
