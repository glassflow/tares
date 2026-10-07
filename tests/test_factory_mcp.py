"""The spec flow's MCP tools (TR-404): what a Claude Code session calls to create a project, write
its docs and tickets, and what the session that builds it calls to read them back.

Drives the tool functions in-process against the daemon (the MCP proxy's HTTP client is pointed
at the ASGI app). Both paths: a project whose tickets live in Tares, and one linked to Linear
(a fake Linear answers, see test_linear.py), where ticket writes are refused but working docs
attach.

Run: .venv/bin/python tests/test_factory_mcp.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tares-factory-mcp-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-factory"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
from tares import linear

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def fake_linear(request: httpx.Request) -> httpx.Response:
    q = json.loads(request.content)["query"]
    if "viewer" in q:
        return httpx.Response(200, json={"data": {"viewer": {"id": "u", "name": "Ada", "email": "a@x",
                                                             "organization": {"name": "Acme", "urlKey": "acme"}}}})
    if "issues(" in q:
        return httpx.Response(200, json={"data": {"project": {"issues": {"nodes": [
            {"id": "i1", "identifier": "FAC-1", "title": "Parse", "url": "https://l/FAC-1",
             "sortOrder": 1, "state": {"name": "Todo", "type": "unstarted"}},
            {"id": "i2", "identifier": "FAC-2", "title": "Store", "url": "https://l/FAC-2",
             "sortOrder": 2, "state": {"name": "Todo", "type": "unstarted"}}],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
    if "project(id" in q:
        return httpx.Response(200, json={"data": {"project": {
            "id": "lp1", "name": "Billing", "url": "https://linear.app/acme/project/billing-aa11", "slugId": "aa11"}}})
    return httpx.Response(400, json={"errors": [{"message": "unexpected"}]})


linear._transport = httpx.MockTransport(fake_linear)


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = "http://t"
    m._cx = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url="http://t", headers=root)
    async with app.router.lifespan_context(app):
        print("== shared docs before any project has docs ==")
        r = await m.read_global_docs()
        ck("read_global_docs says there are none yet", "No shared docs yet" in r, r)

        print("== spec session, tickets in Tares ==")
        out = json.loads(await m.create_project("Invoices", "Turn emailed invoices into rows"))
        ck("create_project -> id, name, goal", out["id"].startswith("uc_")
           and out["goal"] == "Turn emailed invoices into rows", out)
        out2 = await m.create_project("Invoices")
        ck("the same name again is a clear error", out2.startswith("error 4")
           and "Invoices" in out2, out2)
        await m.create_project("Second")   # two projects now: tools must be told which
        r = await m.list_docs()
        ck("with two projects, a tool asks which", "name the project" in r, r)
        spec = json.loads(await m.write_doc("spec", "Spec", "# Build\n\nan invoice parser",
                                            project="Invoices"))
        ck("write_doc -> id", spec["id"].startswith("doc_") and spec["kind"] == "spec", spec)
        for kind, title in (("plan", "Plan"), ("agents", "AGENTS.md"), ("start", "Start here")):
            await m.write_doc(kind, title, f"{kind} text", project="Invoices")
        bad = await m.write_doc("memo", "x", "y", project="Invoices")
        ck("an unknown kind says the kinds", bad.startswith("error 400") and "spec" in bad, bad)
        upd = json.loads(await m.write_doc("spec", "Spec", "# Build\n\nv2", id=spec["id"],
                                           project="Invoices"))
        ck("write_doc with id replaces", upd["id"] == spec["id"], upd)
        t1 = json.loads(await m.write_ticket("Parse invoices", project="Invoices"))
        t2 = json.loads(await m.write_ticket("Store invoices", project="Invoices"))
        ck("write_ticket appends in order", t1["position"] == 1 and t2["position"] == 2, (t1, t2))
        w = json.loads(await m.write_doc("working", "Working: parse", "1. read the PDF",
                                         project="Invoices"))
        a = json.loads(await m.attach_working_doc(t1["id"], w["id"], project="Invoices"))
        ck("attach_working_doc", a["working_doc"] == w["id"], a)
        s = json.loads(await m.write_ticket("", status="in_progress", id=t1["id"],
                                            project="Invoices"))
        ck("write_ticket with id changes status, keeps title", s["status"] == "in_progress"
           and s["title"] == "Parse invoices", s)

        print("== shared docs ==")
        a = json.loads(await m.add_to_global("agents", "Always build on a branch, never commit to main."))
        ck("add_to_global agents", a["added"] and a["doc"] == "AGENTS.md (all projects)", a)
        a = json.loads(await m.add_to_global("memory", "Prices are in euros."))
        ck("add_to_global memory", a["added"], a)
        a = json.loads(await m.add_to_global("memory", "Prices are in euros."))
        ck("the same line twice is not added", a["added"] is False, a)
        bad = await m.add_to_global("notes", "x")
        ck("an unknown shared doc is a clear error", bad.startswith("error 404"), bad)
        g = await m.read_global_docs()
        ck("read_global_docs returns both with the lines",
           "- Always build on a branch" in g and "- Prices are in euros." in g, g)

        print("== the session that builds it ==")
        docs = json.loads(await m.list_docs(project="Invoices"))
        ck("list_docs in reading order, starting prompt first",
           [d["kind"] for d in docs if not d["global"]] == ["start", "spec", "plan", "agents", "working"], docs)
        ck("and the shared docs marked global",
           sorted(d["title"] for d in docs if d["global"])
           == ["AGENTS.md (all projects)", "Memory (all projects)"], docs)
        d = json.loads(await m.get_doc(spec["id"], project="Invoices"))
        ck("get_doc -> body", d["body"] == "# Build\n\nv2"
           and d["updated_by"] == "claude code session", d)
        tl = json.loads(await m.list_tickets(project="Invoices"))
        ck("list_tickets with working doc titles, no Linear",
           tl["linear"] is None and tl["tickets"][0]["working_doc_title"] == "Working: parse", tl)
        g = json.loads(await m.get_ticket(t1["id"], project="Invoices"))
        ck("get_ticket carries the working doc", g["working_doc_body"] == "1. read the PDF", g)
        nf = await m.get_ticket("nope", project="Invoices")
        ck("an unknown ticket is a clear error", nf.startswith("error 404"), nf)

        print("== tickets in Linear ==")
        pid = json.loads(await m.create_project("Billing"))["id"]
        r = await m.link_linear_project("https://linear.app/acme/project/billing-aa11",
                                        project="Billing")
        ck("link before Linear is connected says how", "connect" in r.lower(), r)
        linear._save_connection(app.state.store, {"kind": "api_key", "token": "k",
                                                  "account": {"org": "Acme"}})
        r = json.loads(await m.link_linear_project("https://linear.app/acme/project/billing-aa11",
                                                   project="Billing"))
        ck("link_linear_project -> the tickets found", [t["identifier"] for t in r["tickets"]]
           == ["FAC-1", "FAC-2"] and r["linear"]["name"] == "Billing", r)
        no = await m.write_ticket("x", project="Billing")
        ck("write_ticket refused: change it in Linear", no.startswith("error 409")
           and "Linear" in no, no)
        no = await m.write_ticket("", status="done", id="FAC-1", project="Billing")
        ck("status change refused on a Linear ticket", no.startswith("error 409"), no)
        w = json.loads(await m.write_doc("working", "Working: FAC-1", "steps", project=pid))
        a = json.loads(await m.attach_working_doc("fac-1", w["id"], project="Billing"))
        ck("a working doc attaches to a Linear ticket by identifier", a["identifier"] == "FAC-1"
           and a["working_doc"] == w["id"], a)
        tl = json.loads(await m.list_tickets(project="Billing"))
        ck("list_tickets says where they live", tl["linear"]["name"] == "Billing"
           and tl["tickets"][0]["url"] == "https://l/FAC-1", tl)


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
