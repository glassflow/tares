"""The factory's plan (TR-411, TR-412): milestones with acceptance checks, tickets with their
milestone, number and dependencies, which tickets are ready and what blocks the rest.

Drives the MCP tools in-process against the daemon (the proxy's HTTP client is pointed at the ASGI
app), plus the HTTP routes for what the tools do not cover. A fake Linear answers for the
Linear-linked project: its project milestones and "blocks" relations become the project's
milestones and dependencies.

Run: .venv/bin/python tests/test_factory_plan.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tares-factory-plan-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-plan"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory, linear

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


LIN = {
    "milestones": [{"id": "lm1", "name": "Ingest", "description": "Invoices arrive", "sortOrder": 1},
                   {"id": "lm2", "name": "Report", "description": "", "sortOrder": 2}],
    "issues": [
        {"id": "i1", "identifier": "FAC-1", "title": "Parse", "url": "https://l/FAC-1", "sortOrder": 1,
         "state": {"name": "Todo", "type": "unstarted"}, "projectMilestone": {"id": "lm1"},
         "inverseRelations": {"nodes": []}},
        {"id": "i2", "identifier": "FAC-2", "title": "Store", "url": "https://l/FAC-2", "sortOrder": 2,
         "state": {"name": "Todo", "type": "unstarted"}, "projectMilestone": {"id": "lm1"},
         "inverseRelations": {"nodes": [{"type": "blocks", "issue": {"id": "i1"}},
                                        {"type": "related", "issue": {"id": "i3"}}]}},
        {"id": "i3", "identifier": "FAC-3", "title": "Chart", "url": "https://l/FAC-3", "sortOrder": 3,
         "state": {"name": "Todo", "type": "unstarted"}, "projectMilestone": {"id": "lm2"},
         "inverseRelations": {"nodes": [{"type": "blocks", "issue": {"id": "i2"}}]}},
    ],
}


def fake_linear(request: httpx.Request) -> httpx.Response:
    q = json.loads(request.content)["query"]
    if "viewer" in q:
        return httpx.Response(200, json={"data": {"viewer": {
            "id": "u", "name": "Ada", "email": "a@x", "organization": {"name": "Acme", "urlKey": "acme"}}}})
    if "projectMilestones" in q:
        return httpx.Response(200, json={"data": {"project": {
            "projectMilestones": {"nodes": LIN["milestones"]}}}})
    if "issues(" in q:
        return httpx.Response(200, json={"data": {"project": {"issues": {
            "nodes": LIN["issues"], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
    if "project(id" in q:
        return httpx.Response(200, json={"data": {"project": {
            "id": "lp1", "name": "Billing", "url": "https://linear.app/acme/project/billing-aa11",
            "slugId": "aa11"}}})
    return httpx.Response(400, json={"errors": [{"message": "unexpected"}]})


linear._transport = httpx.MockTransport(fake_linear)


def unit():
    print("== rules ==")
    ck("T3, t3 and 3 are ticket 3", [factory.ticket_number(x) for x in ("T3", "t3", "3")] == [3, 3, 3])
    ck("an id or identifier is not a number", factory.ticket_number("tk_ab") is None
       and factory.ticket_number("FAC-3") is None)
    ck("a loop is found", factory.find_loop({"a": ["b"], "b": ["c"], "c": ["a"]}, "a") == ["a", "b", "c", "a"])
    ck("no loop in a chain", factory.find_loop({"a": ["b"], "b": ["c"], "c": []}, "a") is None)
    try:
        factory.checks_ok([{"expect": "x"}])
        ck("a check without its command is refused", False)
    except factory.DocError:
        ck("a check without its command is refused", True)
    ck("a plain string is a check", factory.checks_ok(["curl /health"]) == [{"check": "curl /health", "expect": ""}])
    ck("depends_on accepts 'T1, T2'", factory.depends_ok("T1, T2 T1") == ["T1", "T2"])


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    cx_factory = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                      base_url="http://t", headers=root)
    m._cx = mf._cx = cx_factory
    async with app.router.lifespan_context(app):
        print("== a plan in Tares ==")
        out = json.loads(await m.create_project("Invoices", "Turn invoices into rows"))
        uid = out["id"]
        r = json.loads(await mf.write_milestone(
            "Ingest", "Invoices from the inbox land as rows",
            [{"check": "curl -s localhost:8000/invoices | jq length", "expect": "> 0"}], project=uid))
        ck("write_milestone -> name, goal, checks", r["name"] == "Ingest" and len(r["checks"]) == 1, r)
        await mf.write_milestone("Report", "A monthly chart", ["open /report"], project=uid)
        again = json.loads(await mf.write_milestone("ingest", "Invoices land as rows",
                                                    [{"check": "make e2e", "expect": "passes"}],
                                                    project=uid))
        ms = json.loads(await mf.list_milestones(project=uid))
        ck("same name changes the milestone, no second one",
           [x["name"] for x in ms] == ["Ingest", "Report"] and ms[0]["goal"] == "Invoices land as rows"
           and ms[0]["checks"] == [{"check": "make e2e", "expect": "passes"}], ms)

        t1 = json.loads(await m.write_ticket("Parse the email", milestone="Ingest", project=uid))
        t2 = json.loads(await m.write_ticket("Store rows", milestone="Ingest", depends_on=["T1"], project=uid))
        t3 = json.loads(await m.write_ticket("Chart", milestone="Report", depends_on=["T2"], project=uid))
        ck("tickets are numbered T1, T2, T3", [t["label"] for t in (t1, t2, t3)] == ["T1", "T2", "T3"],
           (t1, t2, t3))
        ck("write_ticket returns milestone and depends_on",
           t2["milestone_name"] == "Ingest" and t2["depends_on"] == ["T1"], t2)
        bad = await m.write_ticket("Nowhere", milestone="Launch", project=uid)
        ck("an unknown milestone is refused, naming the ones there are",
           "no milestone 'Launch'" in bad and "Ingest" in bad, bad)
        bad = await m.write_ticket("", depends_on=["T3"], id="T1", project=uid)
        ck("a loop is refused and shown", "loop" in bad and "T1 -> T3 -> T2 -> T1" in bad, bad)
        bad = await m.write_ticket("", depends_on=["T1"], id="T1", project=uid)
        ck("a ticket cannot depend on itself", "itself" in bad, bad)
        bad = await m.write_ticket("Ghost", depends_on=["T9"], project=uid)
        ck("an unknown dependency is refused", "no ticket 'T9'" in bad, bad)

        lt = json.loads(await m.list_tickets(project=uid))["tickets"]
        ready = {t["label"]: (t["ready"], t.get("blocked_by")) for t in lt}
        ck("only T1 is ready; T2 waits for T1, T3 for T2",
           ready == {"T1": (True, None), "T2": (False, ["T1"]), "T3": (False, ["T2"])}, ready)
        await m.write_ticket("", status="done", id="T1", project=uid)
        lt = json.loads(await m.list_tickets(project=uid))["tickets"]
        ready = {t["label"]: t["ready"] for t in lt}
        ck("T1 done: T2 is ready, T1 is not (finished)", ready == {"T1": False, "T2": True, "T3": False}, ready)
        g = json.loads(await m.get_ticket("t2", project=uid))
        ck("get_ticket by T-number", g["title"] == "Store rows" and g["milestone_name"] == "Ingest", g)

        ms = json.loads(await mf.list_milestones(project=uid))
        ck("milestones count tickets, done and ready",
           (ms[0]["tickets"], ms[0]["done"], ms[0]["ready"], ms[1]["ready"]) == (2, 1, ["T2"], []), ms)

        async with cx_factory() as cx:
            r = await cx.put(f"/api/projects/{uid}/milestones/Report", json={"name": "Reporting"})
            ck("a milestone can be renamed", r.status_code == 200 and r.json()["name"] == "Reporting", r.text)
            r = await cx.delete(f"/api/projects/{uid}/milestones/Reporting")
            t3now = (await cx.get(f"/api/projects/{uid}/tickets/T3")).json()
            ck("deleting a milestone keeps its tickets, without one",
               r.status_code == 200 and t3now["milestone"] is None, t3now)
            r = await cx.put(f"/api/projects/{uid}/tickets/T3", json={"depends_on": []})
            ck("dependencies can be cleared", r.json()["depends_on"] == [] and r.json()["ready"], r.json())

        print("== a plan in Linear ==")
        async with cx_factory() as cx:
            await cx.post("/api/linear", json={"api_key": "lin_api_x"})
            lp = json.loads(await m.create_project("Billing", "Bill customers"))["id"]
            await mf.write_milestone("Report", "", [{"check": "open /report", "expect": "a chart"}], project=lp)
            r = await cx.post(f"/api/projects/{lp}/linear", json={"project": "Billing"})
            ck("linking runs a first sync", r.status_code == 200, r.text)
        ms = json.loads(await mf.list_milestones(project=lp))
        ck("Linear's milestones show in Linear's order",
           [x["name"] for x in ms] == ["Ingest", "Report"], ms)
        ck("a Tares milestone of the same name became Linear's, keeping its checks",
           ms[1]["checks"] == [{"check": "open /report", "expect": "a chart"}], ms[1])
        ck("goal comes from Linear's description", ms[0]["goal"] == "Invoices arrive", ms[0])
        lt = {t["label"]: t for t in json.loads(await m.list_tickets(project=lp))["tickets"]}
        ck("each ticket has its Linear milestone",
           (lt["FAC-1"].get("milestone_name"), lt["FAC-3"].get("milestone_name")) == ("Ingest", "Report"), lt)
        ck("blocks relations become depends_on (related does not)",
           lt["FAC-2"].get("depends_on") == ["FAC-1"] and lt["FAC-3"].get("depends_on") == ["FAC-2"], lt)
        ck("only FAC-1 is ready", [k for k, t in lt.items() if t["ready"]] == ["FAC-1"], lt)
        bad = await m.write_ticket("", depends_on=[], id="FAC-2", project=lp)
        ck("a Linear ticket's dependencies are changed in Linear", "lives in Linear" in bad, bad)
        bad = await mf.write_milestone("Launch", "", [], project=lp)
        ck("a new milestone on a Linear project is added in Linear", "live in Linear" in bad, bad)
        ok = json.loads(await mf.write_milestone("Ingest", "", [{"check": "make e2e"}], project=lp))
        ck("checks of a Linear milestone are written on Tares", ok["checks"] == [{"check": "make e2e", "expect": ""}], ok)
        async with cx_factory() as cx:
            r = await cx.put(f"/api/projects/{lp}/milestones/Ingest", json={"name": "X"})
            ck("a Linear milestone is renamed in Linear", r.status_code == 409, r.text)

        LIN["milestones"] = LIN["milestones"][:1]
        LIN["issues"][1]["inverseRelations"] = {"nodes": []}
        async with cx_factory() as cx:
            await cx.post(f"/api/projects/{lp}/linear/sync")
        ms = json.loads(await mf.list_milestones(project=lp))
        lt = {t["label"]: t for t in json.loads(await m.list_tickets(project=lp))["tickets"]}
        ck("a milestone gone from Linear goes, its ticket stays without one",
           [x["name"] for x in ms] == ["Ingest"] and not lt["FAC-3"].get("milestone_name"), (ms, lt))
        ck("a relation removed in Linear is removed here", not lt["FAC-2"].get("depends_on"), lt["FAC-2"])
        async with cx_factory() as cx:
            await cx.delete(f"/api/projects/{lp}/linear")
            r = await cx.put(f"/api/projects/{lp}/milestones/Ingest", json={"name": "Intake"})
            ck("after unlinking, milestones are Tares's", r.status_code == 200, r.text)


async def upgrade():
    """Tickets from before numbers get them on the next start, in plan order."""
    print("== numbering survives an upgrade ==")
    from tares.store import Store
    path = os.path.join(_TMP, "old.duckdb")
    s = Store(path)
    a = s.create_ticket("p", "tares", "A", position=2)
    b = s.create_ticket("p", "tares", "B", position=1)
    s.con.execute("UPDATE tickets SET number = NULL")
    s.con.close()
    s = Store(path)
    nums = {t["title"]: t["number"] for t in s.list_tickets("p")}
    ck("old tickets get numbers in plan order", nums == {"B": 1, "A": 2}, nums)
    c = s.create_ticket("p", "tares", "C")
    ck("the next ticket follows", s.get_ticket("p", "T3")["title"] == "C")
    s.con.close()


unit()
asyncio.run(main())
asyncio.run(upgrade())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
