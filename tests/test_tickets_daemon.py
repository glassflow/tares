"""The daemon's ticket edits and link endpoint go through the tickets integration (TR-849).

Runs against a fake Linear (httpx.MockTransport), never the live API. Covered: one link endpoint
(PUT /api/projects/{uid}/tickets/link) for Linear, with the same refusals for GitHub: a body that
carries a key or a token is refused and nothing is linked, unknown fields and a wrong integration
are refused, a project whose tickets live elsewhere answers 409; the old POST link routes are gone;
a title, order and milestone edit of a Linear ticket reaches Linear and shows in Tares, what Linear
refuses is not saved, status, assignee and dependencies stay in Linear (409); a slow write does not
hold the route and lands in the background; the Tares integration refuses a ticket or milestone made
in a linked project and an unknown field on update.

Run: .venv/bin/python tests/test_tickets_daemon.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tares-tickets-daemon-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-tickets-daemon"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import factory_api, linear
from tares.integrations import registry, tickets as tickets_integ

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── the fake Linear ──────────────────────────────────────────────────────────
GOOD_KEY = "lin_api_good"
PROJECT = {"id": "lp-uuid-1", "name": "Invoicing", "url": "https://linear.app/acme/project/invoicing-ab12cd34ef56",
           "slugId": "ab12cd34ef56"}
MILESTONES = [{"id": "lm1", "name": "Alpha", "description": "", "sortOrder": 1.0}]
STATE = {"updates": [], "gate": None, "refuse": False,
         "issues": [
             {"id": "i1", "identifier": "FAC-1", "title": "Parse invoices", "url": "https://linear.app/acme/issue/FAC-1",
              "sortOrder": 1.0, "updatedAt": "2026-10-07T10:00:00Z", "state": {"name": "Todo", "type": "unstarted"}},
             {"id": "i2", "identifier": "FAC-2", "title": "Store invoices", "url": "https://linear.app/acme/issue/FAC-2",
              "sortOrder": 2.0, "updatedAt": "2026-10-07T10:00:00Z", "state": {"name": "Todo", "type": "unstarted"}},
         ]}


async def handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) != linear.API_URL:
        return httpx.Response(404)
    body = json.loads(request.content)
    q, v = body["query"], body.get("variables") or {}
    if "viewer" in q:
        return httpx.Response(200, json={"data": {"viewer": {
            "id": "u1", "name": "Ada", "email": "ada@acme.dev",
            "organization": {"name": "Acme", "urlKey": "acme"}}}})
    if "issueUpdate" in q:
        STATE["updates"].append(v)
        if STATE["gate"] is not None:
            await STATE["gate"].wait()
        ok = not STATE["refuse"]
        return httpx.Response(200, json={"data": {"issueUpdate": {"success": ok, "issue": {"id": v["id"]}}}})
    if "issues(" in q:
        return httpx.Response(200, json={"data": {"project": {"issues": {
            "nodes": STATE["issues"], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
    if "projectMilestones" in q:
        return httpx.Response(200, json={"data": {"project": {"projectMilestones": {
            "nodes": MILESTONES, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
    if "project(id" in q:
        if v["id"] not in (PROJECT["id"], PROJECT["slugId"]):
            return httpx.Response(200, json={"errors": [{"message": "Entity not found: Project"}]})
        return httpx.Response(200, json={"data": {"project": PROJECT}})
    if "projects(" in q:
        n = (v.get("n") or v.get("q") or "").lower()
        nodes = [PROJECT] if not n or n in PROJECT["name"].lower() else []
        return httpx.Response(200, json={"data": {"projects": {"nodes": nodes}}})
    return httpx.Response(400, json={"errors": [{"message": "unexpected query"}]})


linear._transport = httpx.MockTransport(handler)


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": f"Bearer {TOKEN}"})
        r = await cx.post("/api/linear", json={"api_key": GOOD_KEY})
        assert r.status_code == 200, r.text

        async def new_project(name):
            r = await cx.post("/api/projects", json={"name": name, "template": "custom", "objects": []})
            assert r.status_code in (200, 201), r.text
            return r.json()["id"]

        link = lambda uid, body: cx.put(f"/api/projects/{uid}/tickets/link", json=body)

        print("== (A) one endpoint links a project ==")
        uid = await new_project("inv")
        r = await link("nope", {"integration": "linear", "link_fields": {"project": "Invoicing"}})
        ck("(A) an unknown project -> 404", r.status_code == 404, r.text)
        for body, text in [({}, "integration is"), ({"integration": "tares"}, "integration is"),
                           ({"integration": "jira"}, "integration is"),
                           ({"integration": "linear", "link_fields": "Invoicing"}, "dict"),
                           ({"integration": "linear", "link_fields": {"project": "Invoicing"}, "x": 1}, "not: x"),
                           ({"integration": "linear", "link_fields": {"team": "a"}}, "not: team"),
                           ({"integration": "github", "link_fields": {"project": "a"}}, "not: project")]:
            r = await link(uid, body)
            ck(f"(A) {body} -> 400", r.status_code == 400 and text in r.json()["detail"], r.text)
        ck("(A) none of them linked it", store.get_project_linear(uid) is None
           and store.get_project_github(uid) is None)

        print("== (B) a key or a token in link_fields is refused ==")
        for integ, fields in [("linear", {"project": "Invoicing", "api_key": "lin_api_zzz1"}),
                              ("linear", {"project": "Invoicing", "token": "zzz2"}),
                              ("linear", {"project": "Invoicing", "apiKey": "zzz3"}),
                              ("github", {"repo": "acme/app", "token": "ghp_zzz4"}),
                              ("github", {"repo": "acme/app", "github_token": "ghp_zzz5"}),
                              ("github", {"repo": "acme/app", "password": "zzz6"}),
                              ("github", {"repo": "acme/app", "client_secret": "zzz7"})]:
            r = await link(uid, {"integration": integ, "link_fields": fields})
            ck(f"(B) {integ} {sorted(fields)} -> 400 'no secrets'", r.status_code == 400
               and "no secrets" in r.json()["detail"], r.text)
            ck(f"(B) the value is not echoed", all(str(x) not in r.text for x in fields.values()
                                                  if str(x) not in ("Invoicing", "acme/app")), r.text)
        ck("(B) nothing was linked", store.get_project_linear(uid) is None
           and store.get_project_github(uid) is None and not STATE["updates"])

        print("== (C) linking Linear ==")
        r = await link(uid, {"integration": "linear", "link_fields": {"project": "Invoicing"}})
        tickets = r.json().get("tickets") or []
        ck("(C) linked: 200, the link, the tickets read once", r.status_code == 200
           and r.json()["integration"] == "linear" and r.json()["linear"]["name"] == "Invoicing"
           and [t["identifier"] for t in tickets] == ["FAC-1", "FAC-2"]
           and all(t["owner"] == "linear" for t in tickets), r.text)
        ck("(C) the cell's credential never comes back", GOOD_KEY not in r.text)
        r = await link(uid, {"integration": "linear", "link_fields": {"project": "Invoicing"}})
        ck("(C) the same link again -> 200", r.status_code == 200, r.text)
        r = await link(uid, {"integration": "github", "link_fields": {"repo": "acme/app"}})
        ck("(C) a linked project cannot also take GitHub -> 409", r.status_code == 409
           and "live in Linear" in r.json()["detail"] and store.get_project_github(uid) is None, r.text)
        other = await new_project("plain")
        await cx.post(f"/api/projects/{other}/tickets", json={"title": "Mine"})
        r = await link(other, {"integration": "linear", "link_fields": {"project": "Invoicing"}})
        ck("(C) a project with its own tickets -> 409", r.status_code == 409
           and "own tickets" in r.json()["detail"], r.text)
        r = await link(other, {"integration": "github", "link_fields": {"repo": "acme/app"}})
        ck("(C) the same for GitHub -> 409", r.status_code == 409 and "own tickets" in r.json()["detail"], r.text)
        r = await link(await new_project("lost"), {"integration": "linear", "link_fields": {"project": "https://linear.app/acme/nope-000"}})
        ck("(C) a Linear project that is not there -> 400", r.status_code == 400, r.text)

        print("== (D) the old link routes are gone ==")
        r = await cx.post(f"/api/projects/{await new_project('old')}/linear", json={"project": "Invoicing"})
        ck("(D) POST /api/projects/{uid}/linear -> 405", r.status_code == 405, r.status_code)
        r = await cx.post(f"/api/projects/{uid}/github", json={"repo": "acme/app"})
        ck("(D) POST /api/projects/{uid}/github -> 405", r.status_code == 405, r.status_code)
        r = await cx.post(f"/api/projects/{uid}/linear/sync")
        ck("(D) the sync route is unchanged", r.status_code == 200, r.text)

        print("== (E) editing a Linear ticket reaches Linear ==")
        t1 = store.get_ticket(uid, "FAC-1")
        STATE["updates"].clear()
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-1", json={"title": "Parse every invoice"})
        ck("(E) title: 200, shown, and sent to Linear", r.status_code == 200
           and r.json()["title"] == "Parse every invoice" and not r.json().get("edit_pending")
           and STATE["updates"] == [{"id": "i1", "input": {"title": "Parse every invoice"}}], (r.text, STATE["updates"]))
        ck("(E) saved in Tares too", store.get_ticket(uid, "FAC-1")["title"] == "Parse every invoice")
        STATE["updates"].clear()
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-1", json={"position": 7.5})
        ck("(E) position goes to Linear as its order", r.status_code == 200
           and STATE["updates"] == [{"id": "i1", "input": {"sortOrder": 7.5}}]
           and store.get_ticket(uid, "FAC-1")["position"] == 7.5, (r.text, STATE["updates"]))
        STATE["updates"].clear()
        ms = next(m for m in store.list_milestones(uid) if m["name"] == "Alpha")
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-1", json={"milestone": ms["id"]})
        ck("(E) a milestone is sent by its name in Linear", r.status_code == 200
           and STATE["updates"] == [{"id": "i1", "input": {"projectMilestoneId": "lm1"}}]
           and store.get_ticket(uid, "FAC-1")["milestone"] == ms["id"], (r.text, STATE["updates"]))
        STATE["updates"].clear()
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-1", json={"milestone": None})
        ck("(E) no milestone takes it out of its milestone", r.status_code == 200
           and STATE["updates"] == [{"id": "i1", "input": {"projectMilestoneId": None}}]
           and store.get_ticket(uid, "FAC-1")["milestone"] is None, (r.text, STATE["updates"]))
        STATE["updates"].clear()
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-1", json={"title": "Both", "position": 3})
        ck("(E) several fields go in one write", r.status_code == 200 and len(STATE["updates"]) == 1
           and STATE["updates"][0]["input"] == {"title": "Both", "sortOrder": 3.0}, STATE["updates"])

        print("== (F) what is refused, and what Linear refuses, is not saved ==")
        STATE["updates"].clear()
        before = store.get_ticket(uid, "FAC-2")
        for field, value in [("status", "done"), ("assignee", "ada@acme.dev"), ("depends_on", ["FAC-1"])]:
            r = await cx.put(f"/api/projects/{uid}/tickets/FAC-2", json={field: value, "title": "Nope"})
            ck(f"(F) {field} -> 409 'lives in Linear'", r.status_code == 409
               and r.json()["detail"] == f"FAC-2 lives in Linear: change its {field} there", r.text)
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-2", json={"milestone": "ms_nope"})
        ck("(F) a milestone the project does not have -> 404", r.status_code == 404, r.text)
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-2", json={"title": "   "})
        ck("(F) an empty title -> 400", r.status_code == 400, r.text)
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-2", json={"position": "high"})
        ck("(F) a position that is not a number -> 400", r.status_code == 400, r.text)
        ck("(F) none of them sent anything or changed the ticket", not STATE["updates"]
           and store.get_ticket(uid, "FAC-2") == before)
        STATE["refuse"] = True
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-2", json={"title": "Refused"})
        STATE["refuse"] = False
        ck("(F) Linear refusing the write -> an error, and Tares keeps the old title", r.status_code >= 400
           and store.get_ticket(uid, "FAC-2")["title"] == "Store invoices", (r.status_code, r.text))
        r = await cx.post(f"/api/projects/{uid}/docs", json={"kind": "working", "title": "W2", "body": "plan"})
        doc = r.json()["id"]
        STATE["updates"].clear()
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-2", json={"working_doc": doc, "repo": "acme/app"})
        ck("(F) a working doc and a repo stay in Tares and are not sent", r.status_code == 200
           and r.json()["working_doc_body"] == "plan" and r.json()["repo"] == "acme/app"
           and not STATE["updates"], r.text)
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-2", json={})
        ck("(F) an empty body changes nothing and sends nothing", r.status_code == 200 and not STATE["updates"], r.text)

        print("== (G) a slow write does not hold the route ==")
        old_wait = factory_api.EDIT_WAIT
        factory_api.EDIT_WAIT = 0.3
        STATE["gate"] = asyncio.Event()
        STATE["updates"].clear()
        t0 = time.monotonic()
        try:
            r = await asyncio.wait_for(cx.put(f"/api/projects/{uid}/tickets/FAC-2", json={"title": "Slow one"}), 20)
        except asyncio.TimeoutError:
            STATE["gate"].set()
            r = httpx.Response(504, text="the route was held by the slow write")
        took = time.monotonic() - t0
        ck("(G) the route answers after the wait, saying the edit is pending", r.status_code == 200
           and r.json().get("edit_pending") is True and took < 5, (took, r.text))
        ck("(G) the write was started, and Tares has not changed yet", len(STATE["updates"]) == 1
           and store.get_ticket(uid, "FAC-2")["title"] == "Store invoices")
        STATE["gate"].set()
        for _ in range(100):
            if store.get_ticket(uid, "FAC-2")["title"] == "Slow one":
                break
            await asyncio.sleep(0.05)
        ck("(G) when Linear answers the title lands in Tares", store.get_ticket(uid, "FAC-2")["title"] == "Slow one")
        STATE["gate"] = None
        factory_api.EDIT_WAIT = old_wait

        print("== (H) Tares tickets: a linked project takes none, an unknown field is refused ==")
        be = registry.get("tickets", "tares")(store)
        project = store.get_project(uid)
        for what, kw in [("ticket", {"milestones": [], "tickets": [{"title": "Mine"}]}),
                         ("milestone", {"milestones": [{"name": "Beta"}], "tickets": []})]:
            try:
                await be.create(project, **kw)
                ck(f"(H) a {what} made in a Linear project is refused", False)
            except tickets_integ.LinkConflict as e:
                ck(f"(H) a {what} made in a Linear project is refused", "live in Linear" in str(e), str(e))
        ck("(H) and nothing was added", len([t for t in store.list_tickets(uid) if t["owner"] == "tares"]) == 0
           and store.get_milestone(uid, "Beta") is None)
        refs = await be.create(project, milestones=[{"name": "Alpha", "goal": "Ship it"}], tickets=[])
        ck("(H) a milestone that is already there is still changed, not refused", len(refs) == 1
           and store.get_milestone(uid, "Alpha")["goal"] == "Ship it")
        plain = other
        tt = store.get_ticket(plain, "T1")
        try:
            await be.update(tt, {"title": "Fine", "colour": "red"})
            ck("(H) an unknown field on a Tares ticket is refused", False)
        except ValueError as e:
            ck("(H) an unknown field on a Tares ticket is refused, naming it", "colour" in str(e), str(e))
        ck("(H) and the ticket keeps its title", store.get_ticket(plain, "T1")["title"] == "Mine")
        await be.update(tt, {"title": "Renamed"})
        ck("(H) the known fields still change it", store.get_ticket(plain, "T1")["title"] == "Renamed")

        print("== (I) unlinking is unchanged ==")
        r = await cx.delete(f"/api/projects/{uid}/linear")
        ck("(I) DELETE unlinks", r.status_code == 200 and store.get_project_linear(uid) is None, r.text)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
