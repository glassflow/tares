# Tares enterprise licence, see ee/LICENSE
"""The ee MCP tools of a factory cell follow the licence (TR-923).

A factory cell with a licence lists the four outcome tools first, then the question tools the ee
team adds (`ask_area`, `answer_question`, `list_questions`), and a call to one of them runs. The same
cell with no licence does not list them and answers a call with the licence text. `sign_off` is not
a tool: the cell has no sign-offs.

The core side of the same tools is tests/test_mcp_editions.py and tests/test_handover_single.py.

Run: python ee/tests/test_mcp_editions_ee.py
"""
import asyncio, json, os, signal, socket, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed
from _env import clean_env

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


TOKEN = "root-mcp-ee"
ROOT = {"Authorization": f"Bearer {TOKEN}"}
OUTCOMES = ["what_needs_me", "how_is_project", "start_project", "hand_project_to_crew"]
QUESTION_TOOLS = ["ask_area", "answer_question", "list_questions"]
LICENCE_TEXT = "licence_required: This needs an enterprise licence. https://docs.glassflow.ai/tares/enterprise"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


async def wait(url, ok=lambda r: r.status_code == 200):
    for _ in range(120):
        try:
            async with httpx.AsyncClient() as cx:
                if ok(await cx.get(url, timeout=1)):
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


def start(licence):
    dport, mport = free_port(), free_port()
    tmp = tempfile.mkdtemp(prefix="mcp-editions-ee-")
    base = clean_env(TARES_OTLP_GRPC_PORT="off", TARES_AUTH_TOKEN=TOKEN, TARES_EDITION="factory",
                     TARES_LICENSE=licensed.test_licence() if licence else None)
    d = subprocess.Popen(licensed.daemon_argv(),
                         env={**base, "TARES_DB": os.path.join(tmp, "t.duckdb"), "TARES_CATALOG": "/tmp/none_me.yaml",
                              "TARES_PORT": dport},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    m = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_mcp; run_mcp()"],
                         env={**base, "TARES_MCP_TRANSPORT": "streamable-http", "TARES_MCP_HOST": "127.0.0.1",
                              "TARES_MCP_PORT": mport, "TARES_MCP_SURFACE_TTL": "0",
                              "TARESD_URL": f"http://127.0.0.1:{dport}"},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return d, m, f"http://127.0.0.1:{dport}", f"http://127.0.0.1:{mport}/mcp"


def stop(*procs):
    for p in procs:
        p.send_signal(signal.SIGTERM)
        try: p.wait(timeout=5)
        except Exception: p.kill()


async def session(url, fn):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    async with streamablehttp_client(url, headers=ROOT) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await fn(s)


async def names(url):
    async def f(s):
        return [t.name for t in (await s.list_tools()).tools]
    return await session(url, f)


async def call(url, name, args):
    async def f(s):
        return (await s.call_tool(name, args)).content[0].text
    return await session(url, f)


async def licensed_cell():
    print("== a factory cell with a licence ==")
    d, m, durl, murl = start(True)
    try:
        if not (await wait(f"{durl}/health") and await wait(murl, ok=lambda r: r.status_code < 500)):
            ck("daemon and mcp up", False); return
        n = await names(murl)
        ck("the outcome tools come first", n[:4] == OUTCOMES, str(n[:6]))
        ck("the question tools are listed", all(t in n for t in QUESTION_TOOLS), str(n))
        ck("they come after the outcome tools", all(t in n and n.index(t) >= 4 for t in QUESTION_TOOLS), str(n[:8]))
        ck("sign_off is not a tool", "sign_off" not in n, str(n))
        ck("the crew's tools stay", "list_tickets" in n and "hand_over" in n and "my_work" in n, str(n))
        out = await call(murl, "list_questions", {})
        ck("list_questions runs and is not the licence text", out != LICENCE_TEXT and "licence" not in out.lower(), out)
        out = await call(murl, "ask_area", {"repo": "acme/web", "text": "Which colour?"})
        ck("ask_area runs: with no project it says so, not the licence text", out != LICENCE_TEXT, out)
        out = await call(murl, "start_project", {"name": "alpha", "goal": "ship the thing"})
        ck("a project is made", '"name": "alpha"' in out, out)
        out = await call(murl, "ask_area", {"repo": "acme/web", "text": "Which colour?", "project": "alpha"})
        ck("ask_area to a repo nobody owns is refused in plain words",
           "acme/web has no owner on this cell" in out and "—" not in out, out)
    finally:
        stop(m, d)


async def unlicensed_cell():
    print("== the same cell with no licence ==")
    d, m, durl, murl = start(False)
    try:
        if not (await wait(f"{durl}/health") and await wait(murl, ok=lambda r: r.status_code < 500)):
            ck("daemon and mcp up", False); return
        n = await names(murl)
        ck("the outcome tools come first", n[:4] == OUTCOMES, str(n[:6]))
        ck("no question tool is listed", not set(QUESTION_TOOLS) & set(n), str(sorted(set(QUESTION_TOOLS) & set(n))))
        for t in QUESTION_TOOLS:
            args = {"repo": "acme/web", "text": "x"} if t == "ask_area" else (
                {"id": "q", "answer": "x"} if t == "answer_question" else {})
            ck(f"a call to {t} answers the licence text", await call(murl, t, args) == LICENCE_TEXT)
    finally:
        stop(m, d)


asyncio.run(licensed_cell())
asyncio.run(unlicensed_cell())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
