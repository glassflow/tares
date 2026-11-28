"""The outcome tools (TR-757, TR-1813): a cell lists its outcome tools first and every other tool it
defines after them, a crew station too, and an unreachable daemon still lists every tool. The AI SRE
tools are gone (TR-1809), and so are the building-block tools of the source catalog (TR-1811).
The list is tidy, not a permission boundary: the daemon still checks every call.

Run: python tests/test_mcp_outcome_tools.py
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
           "discover_docker", "test_source", "create_source", "list_sources", "source_fields")
OUTCOMES = ("what_needs_me", "how_is_project", "start_project", "hand_project_to_crew")
SRE_OUTCOMES = ("what_is_broken", "how_is_service", "what_happened", "mark_incident")


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


def _start(daemon=True):
    tmp = tempfile.mkdtemp(prefix="mcp-outcome-tools-")
    base = clean_env(TARES_AUTH_TOKEN=TOKEN)
    d = None
    if daemon:
        d = _spawn("from tares.cli import run_daemon; run_daemon()",
                   {**base, "TARES_HOME": os.path.join(tmp, "t"), "TARES_PORT": DPORT})
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
    print("== a cell ==")
    d, m = _start()
    try:
        if not await up():
            ck("daemon and mcp up", False); return
        n = await names()
        ck("the outcome tools come first", n[:4] == list(OUTCOMES), str(n[:6]))
        ck("no building-block tool (gone, TR-1811)", not set(OBJECTS) & set(n), str(sorted(set(OBJECTS) & set(n))))
        ck("answer_question is an ee tool: not listed without a licence", "answer_question" not in n)
        ck("the crew's tools stay", "list_tickets" in n and "desk_list" in n and "hand_over" in n, str(n))
        st = {"X-Tares-Role": "builder"}
        sn = await names({**ROOT, **st})
        ck("a station sees no building block either", not set(OBJECTS) & set(sn) and "list_tickets" in sn,
           str(sorted(set(OBJECTS) & set(sn))))
        ck("a station sees the same list as a person", sorted(sn) == sorted(n), str(set(sn) ^ set(n)))

        started = await call("start_project", {"name": "alpha", "goal": "ship the thing"})
        ck("start_project makes a project", '"name": "alpha"' in started, started)
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            r = await cx.post("/api/desk", json={"question": "Which colour for the links?",
                                                 "recommendation": "blue", "why": "it reads well",
                                                 "project": "alpha"})
            ck("a question on the desk", r.status_code == 201, r.text)
            w = (await cx.get("/api/whoami")).json()
            ck("whoami no longer says whether object tools are on", "mcp_object_tools" not in w, str(w))
            r = await cx.get("/api/settings/mcp")
            ck("the object tools setting is gone", r.status_code == 404, r.text)
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
        # whose turn each open ticket is: the person's first, a closed one left out
        async with httpx.AsyncClient(base_url=D, headers=ROOT, timeout=10) as cx:
            tk = f"/api/projects/{row['id']}/tickets"
            refs = []
            for body in ({"title": "Build the page"}, {"title": "Wire the API"},
                         {"title": "Pick the font"}, {"title": "Old idea", "status": "done"}):
                r = await cx.post(tk, json=body)
                refs.append(r.json().get("ref") or r.json().get("label"))
            ck("four tickets made", all(refs), str(refs))
            # the stuck one is moved first, so newest first alone would list it last
            r = await cx.post(f"{tk}/{refs[2]}/stage", json={"stage": "blocked", "reason": "no licence for the font"})
            ck("the font is stuck", r.status_code == 200, r.text)
            r = await cx.put(f"{tk}/{refs[1]}", json={"depends_on": [refs[0]]})
            ck("the API waits on the page", r.status_code == 200, r.text)
            r = await cx.put(f"{tk}/{refs[0]}", json={"title": "Build the page, first cut"})
            ck("the page is touched last", r.status_code == 200, r.text)
        row = json.loads(await call("how_is_project", {"project": "alpha"}))
        open_ = row.get("open_tickets") or []
        turns = [(t["ref"], t["turn"], t["whose"]) for t in open_]
        ck("how_is_project lists the open tickets, yours first, the closed one left out",
           [x[0] for x in turns][:1] == [refs[2]] and sorted(x[0] for x in turns) == sorted(refs[:3]), str(turns))
        by = {t["ref"]: t for t in open_}
        ck("each reads its turn in the console's words",
           by.get(refs[2], {}).get("turn_label") == "Stuck"
           and by.get(refs[0], {}).get("turn_label") == "To do"
           and by.get(refs[1], {}).get("turn_label") == f"Queued after {refs[0]}", str(open_))
        ck("and how many are with each", row.get("whose_turn") == {"you": 1, "crew": 1, "checks": 0, "waiting": 1}
           and row.get("open_tickets_cut") is False, str(row.get("whose_turn")))
        miss = await call("how_is_project", {"project": "nope"})
        ck("an unknown project names the ones there are", miss == "No project called nope. Projects: alpha.", miss)
        sent = await call("hand_project_to_crew", {"project": "alpha", "repos": [
            {"path": "/tmp/alpha", "name": "acme/alpha"}]})
        ck("hand_project_to_crew records the hand-over", sent.startswith("Recorded") and "/tmp/alpha" in sent, sent)

        ck("a bad key still lists every tool and the call says 401",
           set(OUTCOMES) <= set(await names({"Authorization": "Bearer nope"})))
        ck("what_needs_me reports an error from the daemon",
           (await call("what_needs_me", {}, {"Authorization": "Bearer nope"})).startswith("error 401"))
    finally:
        _stop(m, d)


async def unreachable():
    print("== the daemon is unreachable ==")
    d, m = _start(daemon=False)
    try:
        if not await up(daemon=False):
            ck("mcp up", False); return
        n = await names()
        ck("it lists every tool", set(OUTCOMES) <= set(n) and "list_tickets" in n, str(n))
    finally:
        _stop(m, d)


def words():
    print("== the outcome tools follow the words list ==")
    from tares import mcp_server, words as w
    tools = {t.name: t for t in mcp_server.mcp._tool_manager.list_tools()}
    ck("all four are registered", set(OUTCOMES) == set(mcp_server.OUTCOME_TOOLS) <= set(tools))
    bad = [(n, w.offenders(n) + w.offenders(tools[n].description or "")) for n in mcp_server.OUTCOME_TOOLS]
    ck("no forbidden word or em dash in a name or description", not any(b for _, b in bad), str(bad))
    ck("the AI SRE tools are gone (TR-1809)", not set(SRE_OUTCOMES) & set(tools), str(sorted(set(SRE_OUTCOMES) & set(tools))))


async def main():
    words()
    await factory()
    await unreachable()
    print(f"\n{P} passed, {F} failed")

asyncio.run(main())
sys.exit(1 if F else 0)
