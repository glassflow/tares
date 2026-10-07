"""A session's recording lands on its project (TR-405).

The plugin side: which tool calls tie a session to a project (create_project by name, a Tares tool
naming `project`; not another server's tool), the stamp on every line from then on, the synthetic
session_project line, and a new project switching it. The Tares side: a session_project line ties
the session to the project, the claude_code source joins the project, and the project lists the
session with every line, the brainstorm from before the project existed included.

Run: .venv/bin/python tests/test_session_project.py
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "claude-plugin", "scripts"))

_TMP = tempfile.mkdtemp(prefix="tares-session-project-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-sp"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import ship

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


SID = "sess-1234"


def line(i, text=None, tool=None, inp=None):
    o = {"type": "assistant" if tool else "user", "sessionId": SID, "cwd": "/w/invoices",
         "timestamp": f"2026-10-07T10:00:{i:02d}.000Z", "uuid": f"u{i}"}
    if tool:
        o["message"] = {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": tool, "input": inp or {}}]}
    else:
        o["message"] = {"role": "user", "content": text or f"line {i}"}
    return o


def plugin():
    print("== the plugin ==")
    ck("create_project names the project",
       ship.project_call(line(1, tool="mcp__tares__create_project", inp={"name": "Invoices"}))
       == "Invoices")
    ck("from the plugin's server too",
       ship.project_call(line(1, tool="mcp__plugin_tares_tares__write_doc",
                              inp={"project": "Invoices", "kind": "spec"})) == "Invoices")
    ck("a Tares tool without a project says nothing",
       ship.project_call(line(1, tool="mcp__tares__list_projects")) is None)
    ck("another server's create_project is not ours",
       ship.project_call(line(1, tool="mcp__linear__create_project", inp={"name": "X"})) is None)
    ck("a plain line says nothing", ship.project_call(line(1, "hello")) is None)
    hook = {"session_id": SID, "cwd": "/w/invoices"}
    objs = [line(1, "let's build an invoice parser"), line(2, "what about emails?"),
            line(3, tool="mcp__tares__create_project", inp={"name": "Invoices"}),
            line(4, "now the spec")]
    out, project, changed = ship.stamp_project(objs, "", hook)
    ck("changed to the new project", changed and project == "Invoices")
    ck("lines before it are not stamped (Tares ties them by session)",
       "tares_project" not in out[0] and "tares_project" not in out[1])
    ck("the call and every line after are stamped",
       out[2]["tares_project"] == "Invoices" and out[4]["tares_project"] == "Invoices")
    sp = out[3]
    ck("a session_project line right after the call",
       sp["type"] == "session_project" and sp["tares_project"] == "Invoices"
       and sp["sessionId"] == SID, sp)
    out2, project2, changed2 = ship.stamp_project([line(5, "more")], "Invoices", hook)
    ck("the next batch keeps the project without a new line",
       not changed2 and len(out2) == 1 and out2[0]["tares_project"] == "Invoices")
    out3, project3, changed3 = ship.stamp_project(
        [line(6, tool="mcp__tares__get_doc", inp={"project": "Billing", "id": "d"})], "Invoices",
        hook)
    ck("naming another project switches it", changed3 and project3 == "Billing"
       and out3[1]["type"] == "session_project")
    d = tempfile.mkdtemp()
    ship.write_project(d, SID, "Invoices")
    ck("the marker round-trips", ship.read_project(d, SID) == "Invoices"
       and ship.read_project(d, "other") == "")
    return out


async def tares(shipped):
    print("== Tares ==")
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": f"Bearer {TOKEN}"})
        r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}})
        ck("claude_code source", r.status_code in (200, 201), r.text)
        uid = (await cx.post("/api/projects", json={"template": "custom", "name": "Invoices",
                                                    "objects": []})).json()["id"]
        nd = "\n".join(json.dumps(o) for o in shipped) + "\n"
        r = await cx.post("/ingest/claude_code", content=nd,
                          headers={"Content-Type": "application/x-ndjson"})
        ck("ingest the session", r.status_code == 202, r.text)
        r = await cx.get(f"/api/projects/{uid}/sessions")
        ss = r.json()["sessions"]
        ck("the project lists the session with its repo", len(ss) == 1 and ss[0]["session"] == SID
           and ss[0]["repo"] == "invoices", ss)
        ck("every line counts, the brainstorm before the project included",
           ss[0]["lines"] == len(shipped), ss)
        r = await cx.get(f"/api/projects/{uid}/sessions/{SID}")
        texts = [x["text"] for x in r.json()["lines"]]
        ck("read the session top to bottom", texts[0] == "let's build an invoice parser"
           and "session works in project Invoices" in texts, texts)
        ck("the claude_code source is in the project", "claude_code" in store.project_sources(uid))
        r = await cx.get(f"/api/projects/{uid}/sessions/other")
        ck("a session that did not work here -> 404", r.status_code == 404, r.text)
        r = await cx.post("/ingest/claude_code", content=json.dumps(
            {"type": "session_project", "sessionId": "s2", "tares_project": "Nope",
             "timestamp": "2026-10-07T10:01:00.000Z"}) + "\n",
            headers={"Content-Type": "application/x-ndjson"})
        ck("an unknown project is ignored, the line still lands", r.status_code == 202
           and store.session_projects("s2") == [], r.text)
        await cx.delete(f"/api/projects/{uid}")
        ck("deleting the project forgets its sessions", store.session_projects(SID) == [])
        await cx.aclose()


if __name__ == "__main__":
    shipped = plugin()
    asyncio.run(tares(shipped))
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
