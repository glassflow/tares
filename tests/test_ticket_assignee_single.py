"""Core with no licence: ticket assignees belong to ee (TR-920).

Without an ee team, a Linear or GitHub sync never writes an assignee (the mapper answers None,
which means "not synced", not "no assignee"), so an assignee already stored stays and a new ticket
gets none. A route or write_ticket that names an assignee answers 402 `licence_required`; clearing
one stays allowed. Showing a stored assignee is a read and stays in core: the ticket JSON still
carries `assignee` {id, name} or `assignee_raw`, and the MCP text still says who owns it.

The licensed behaviour of the same paths is ee/tests/test_ticket_assignee.py. The Linear and GitHub
fakes below answer like their GraphQL endpoints, never the live APIs.

Run: .venv/bin/python tests/test_ticket_assignee_single.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from _env import scrub_os_environ
scrub_os_environ()

TMP = tempfile.mkdtemp(prefix="tares-assignee-single-")
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = AUTH = "root-token-assignee-single"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import team as team_mod

team_mod._set_ee_for_tests(None)   # no ee team, whatever is installed on this machine

from tares import github_tickets as gt, linear, mcp_server as ms
from tares.daemon import make_app

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def exact_402(r):
    b = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return (r.status_code == 402 and set(b) == {"code", "message", "url"} and b["code"] == "licence_required"
            and "enterprise licence" in b["message"] and "—" not in b["message"])


# the fake Linear: it sends `assignee` whenever the query asks for it
LIN_PROJECT = {"id": "lp-uuid-1", "name": "Invoicing",
               "url": "https://linear.app/acme/project/invoicing-ab12cd34ef56", "slugId": "ab12cd34ef56"}
LIN = {"assignees": {}}


def lin_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) != linear.API_URL:
        return httpx.Response(404)
    body = json.loads(request.content)
    q = body["query"]
    if "issues(" in q:
        nodes = []
        for n in range(1, 4):
            node = {"id": f"i{n}", "identifier": f"FAC-{n}", "title": f"Issue {n}" + LIN.get("suffix", ""),
                    "url": f"https://linear.app/acme/issue/FAC-{n}", "sortOrder": float(n),
                    "updatedAt": "2026-10-07T10:00:00Z" if not LIN.get("suffix") else "2026-10-08T10:00:00Z",
                    "state": {"name": "Todo", "type": "unstarted"}}
            if "assignee { email name }" in q and node["id"] in LIN["assignees"]:
                node["assignee"] = LIN["assignees"][node["id"]]
            nodes.append(node)
        return httpx.Response(200, json={"data": {"project": {"issues": {
            "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
    if "projectMilestones" in q:
        return httpx.Response(200, json={"data": {"project": {"projectMilestones": {"nodes": []}}}})
    if "project(id" in q:
        return httpx.Response(200, json={"data": {"project": LIN_PROJECT}})
    if "projects(" in q:
        return httpx.Response(200, json={"data": {"projects": {"nodes": [LIN_PROJECT]}}})
    return httpx.Response(400, json={"errors": [{"message": "unexpected query"}]})


linear._transport = httpx.MockTransport(lin_handler)

# the fake GitHub: it sends `assignees` whenever the query asks for it
GH = {"assignees": {}}
LABEL = "tares:Gh project"


def gh_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) != gt.API_URL:
        return httpx.Response(404)
    q = json.loads(request.content)["query"]
    nodes = []
    for n in range(1, 4):
        node = {"id": f"I_{n}", "number": n, "title": f"Gh issue {n}" + GH.get("suffix", ""),
                "url": f"https://github.com/acme/app/issues/{n}", "state": "OPEN", "stateReason": None,
                "labels": {"nodes": [{"name": LABEL}]}, "milestone": None, "blockedBy": {"nodes": []}}
        if "assignees(first: 1)" in q:
            node["assignees"] = {"nodes": [{"login": x} for x in GH["assignees"].get(n, [])]}
        nodes.append(node)
    return httpx.Response(200, json={"data": {"rateLimit": {"remaining": 4999, "resetAt": "x"},
        "repository": {"nameWithOwner": "acme/app", "url": "https://github.com/acme/app",
                       "milestones": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                       "issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})


gt._transport = httpx.MockTransport(gh_handler)


def structure():
    print("== the module moved ==")
    ck("tares/ has no ticket_assignee module any more",
       not os.path.exists(os.path.join(ROOT, "tares", "ticket_assignee.py")))
    t = team_mod.get()
    ck("the single-person team does not sync assignees", t.assignee_from_linear(None, {"email": "a@x.dev"}) is None
       and t.assignee_from_github(None, {"assignees": [{"login": "a"}]}) is None)
    ck("an empty ref resolves to nobody, a named one needs the licence", t.assignee_resolve(None, "") == (None, None))
    try:
        t.assignee_resolve(None, "a@x.dev")
        raised = False
    except Exception as e:
        raised = type(e).__name__ == "LicenceRequired"
    ck("naming one raises LicenceRequired", raised)


def note():
    print("== the MCP text for a stored assignee ==")
    ck("a person", ms._assignee_note({"assignee": {"id": "p_1", "name": "Kiran"}}) == "assignee: Kiran")
    ck("unowned with a raw name", ms._assignee_note({"assignee": None, "assignee_raw": "k@x.dev"})
       == "unowned (k@x.dev is not on this cell)")
    ck("unowned", ms._assignee_note({"assignee": None, "assignee_raw": None}) == "unowned")


async def main():
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=H(AUTH))

        async def tickets(uid):
            r = await cx.get(f"/api/projects/{uid}/tickets")
            return {t["number"]: t for t in r.json()["tickets"]}

        kiran = store.create_person("Kiran", "kiran@x.dev", github="kiran-gh")["id"]
        lena = store.create_person("Lena", "lena@x.dev")["id"]
        linear._save_connection(store, {"kind": "api_key", "token": "lin_api_good", "account": {"name": "Ada"}})
        store.upsert_github_credential("cred-a", "tok-a")

        print("== Linear sync: no assignee is written ==")
        LIN["assignees"] = {"i1": {"email": "kiran@x.dev", "name": "Kiran"},
                            "i2": {"email": "ghost@x.dev", "name": "Ghost"}}
        uid = (await cx.post("/api/projects", json={"template": "custom", "name": "Invoices",
                                                    "objects": []})).json()["id"]
        r = await cx.post(f"/api/projects/{uid}/linear", json={"project": LIN_PROJECT["url"]})
        ck("link answers 200", r.status_code == 200, r.text[:300])
        tk = await tickets(uid)
        ck("the tickets came in", sorted(tk) == [1, 2, 3] and all(t["owner"] == "linear" for t in tk.values()),
           list(tk))
        ck("a person's email in Linear gives the ticket no assignee",
           tk[1]["assignee"] is None and tk[1]["assignee_raw"] is None, tk[1])
        ck("an unknown email is not kept as raw either", tk[2]["assignee"] is None
           and tk[2]["assignee_raw"] is None, tk[2])
        ck("and an unassigned ticket stays unassigned", tk[3]["assignee"] is None and tk[3]["assignee_raw"] is None)
        t1 = store.get_ticket(uid, "FAC-1")
        store.update_ticket(t1["id"], assignee_person_id=kiran)
        store.update_ticket(store.get_ticket(uid, "FAC-2")["id"], assignee_raw="ghost@x.dev")
        LIN["assignees"]["i1"] = {"email": "lena@x.dev", "name": "Lena"}      # reassigned in Linear
        del LIN["assignees"]["i2"]                                            # unassigned in Linear
        LIN["suffix"] = " (edited)"                                           # a real change, so the row is rewritten
        await linear.sync_project(store, uid)
        tk = await tickets(uid)
        ck("a sync that changed the title rewrote the ticket", tk[1]["title"].endswith("(edited)"), tk[1]["title"])
        ck("a stored assignee survives a sync that says someone else",
           store.get_ticket(uid, "FAC-1")["assignee_person_id"] == kiran, store.get_ticket(uid, "FAC-1"))
        ck("and still shows in the ticket output", tk[1]["assignee"] == {"id": kiran, "name": "Kiran"}, tk[1])
        ck("a stored raw assignee survives a sync that says nobody",
           store.get_ticket(uid, "FAC-2")["assignee_raw"] == "ghost@x.dev"
           and tk[2]["assignee_raw"] == "ghost@x.dev", tk[2])

        print("== GitHub sync: no assignee is written ==")
        gid = (await cx.post("/api/projects", json={"template": "custom", "name": "Gh project",
                                                    "objects": []})).json()["id"]
        GH["assignees"] = {1: ["kiran-gh"], 2: ["Octo-Unknown"]}
        r = await cx.post(f"/api/projects/{gid}/github", json={"repo": "acme/app", "issue_label": LABEL})
        ck("link answers 200", r.status_code == 200, r.text[:300])
        tk = await tickets(gid)
        ck("the issues came in as GitHub tickets", sorted(tk) == [1, 2, 3]
           and all(t["owner"] == "github" for t in tk.values()), list(tk))
        ck("a login that matches a person gives no assignee", tk[1]["assignee"] is None
           and tk[1]["assignee_raw"] is None, tk[1])
        ck("an unknown login is not kept as raw", tk[2]["assignee"] is None and tk[2]["assignee_raw"] is None, tk[2])
        store.update_ticket(store.get_ticket(gid, "GH-3")["id"], assignee_person_id=lena)
        GH["assignees"] = {3: ["kiran-gh"]}
        GH["suffix"] = " (edited)"
        await gt.sync_project(store, gid)
        tk = await tickets(gid)
        ck("a sync that changed the title rewrote the ticket", tk[3]["title"].endswith("(edited)"), tk[3]["title"])
        ck("a stored assignee survives a GitHub sync that says someone else",
           tk[3]["assignee"] == {"id": lena, "name": "Lena"}, tk[3])

        print("== private project: a sync adds nobody to it ==")
        store.set_project_scope(uid, "private")
        LIN["assignees"] = {"i3": {"email": "lena@x.dev", "name": "Lena"}}
        LIN["suffix"] = " (again)"
        await linear.sync_project(store, uid)
        ck("an assignee in Linear brings no member in", store.list_project_members(uid) == []
           or all(m["reason"] != "ticket" for m in store.list_project_members(uid)), store.list_project_members(uid))

        print("== routes: naming an assignee needs the licence ==")
        team = (await cx.post("/api/projects", json={"template": "custom", "name": "Team work",
                                                     "objects": []})).json()["id"]
        r = await cx.post(f"/api/projects/{team}/tickets", json={"title": "A", "assignee": kiran})
        ck("create with a person id: 402 licence_required", exact_402(r), f"{r.status_code} {r.text[:120]}")
        r = await cx.post(f"/api/projects/{team}/tickets", json={"title": "B", "assignee": "kiran@x.dev"})
        ck("create with an email: 402", exact_402(r), f"{r.status_code} {r.text[:120]}")
        ck("and no ticket was made", len(await tickets(team)) == 0)
        r = await cx.post(f"/api/projects/{team}/tickets", json={"title": "C"})
        ck("create without an assignee works", r.status_code == 201 and r.json()["assignee"] is None, r.text[:200])
        c_id = r.json()["id"]
        r = await cx.post(f"/api/projects/{team}/tickets", json={"title": "D", "assignee": None})
        ck("create with `assignee: null` works", r.status_code == 201, r.text[:200])
        r = await cx.post(f"/api/projects/{team}/tickets", json={"title": "E", "assignee": ""})
        ck("create with an empty assignee works", r.status_code == 201 and r.json()["assignee"] is None, r.text[:200])
        r = await cx.put(f"/api/projects/{team}/tickets/{c_id}", json={"assignee": lena})
        ck("update naming one: 402", exact_402(r), f"{r.status_code} {r.text[:120]}")
        ck("and nothing changed", store.get_ticket(team, c_id)["assignee_person_id"] is None)
        r = await cx.put(f"/api/projects/{team}/tickets/{c_id}", json={"title": "C2"})
        ck("an update that does not say assignee still works", r.status_code == 200, r.text[:200])

        print("== a stored assignee can still be cleared, and still shows ==")
        store.update_ticket(store.get_ticket(team, c_id)["id"], assignee_person_id=lena)
        r = await cx.get(f"/api/projects/{team}/tickets/{c_id}")
        ck("a stored assignee shows as {id, name}", r.json()["assignee"] == {"id": lena, "name": "Lena"}
           and r.json()["assignee_raw"] is None, r.text[:200])
        r = await cx.put(f"/api/projects/{team}/tickets/{c_id}", json={"title": "C3"})
        ck("an update that does not say assignee leaves it", r.json()["assignee"]["id"] == lena, r.text[:200])
        r = await cx.put(f"/api/projects/{team}/tickets/{c_id}", json={"assignee": None})
        ck("`assignee: null` clears it", r.status_code == 200 and r.json()["assignee"] is None
           and r.json()["assignee_raw"] is None, r.text[:200])
        store.update_ticket(store.get_ticket(team, c_id)["id"], assignee_person_id=lena)
        r = await cx.put(f"/api/projects/{team}/tickets/{c_id}", json={"assignee": ""})
        ck("an empty string clears it", r.status_code == 200 and r.json()["assignee"] is None, r.text[:200])
        store.update_ticket(store.get_ticket(team, c_id)["id"], assignee_raw="who@x.dev")
        r = await cx.get(f"/api/projects/{team}/tickets/{c_id}")
        ck("a stored raw assignee shows as raw", r.json()["assignee"] is None
           and r.json()["assignee_raw"] == "who@x.dev", r.text[:200])

        print("== write_ticket ==")
        ms.TARESD = "http://t"
        ms._cx = lambda timeout=10: httpx.AsyncClient(timeout=timeout, headers=H(AUTH),
                                                      transport=httpx.ASGITransport(app=app))
        bad = await ms.write_ticket("From MCP", assignee="lena@x.dev", project="Team work")
        ck("naming an assignee comes back as the licence error", bad.startswith("error 402")
           and "licence" in bad, bad)
        out = json.loads(await ms.write_ticket("From MCP", project="Team work"))
        ck("without one it writes", out["assignee"] is None, out)
        listed = json.loads(await ms.list_tickets("Team work"))
        ck("list_tickets still carries assignee_note", all("assignee_note" in t for t in listed["tickets"]), listed)


structure()
note()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
