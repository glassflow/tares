"""Who a ticket is for (TR-631): every ticket has an assignee person, or shows as unowned with the
assignee as its source named them, wherever the ticket lives.

Runs against a fake Linear and a fake GitHub (httpx.MockTransport answering like their GraphQL
endpoints), never the live APIs. Covers: Linear's assignee mapped by email (case ignored) and by a
person's extra email; an email nobody has -> unowned with the email as raw; a null email (a key
that cannot read emails) -> unowned with the name as raw, and one warning per sync; a person added
later is picked up by the next sync; GitHub's first assignee mapped by login, an unknown login as
raw; Tares tickets taking `assignee` (a person id or an email) from the routes and write_ticket,
an unknown email kept as raw, junk refused, a Linear ticket refusing it; a person assigned to a
ticket of a private project joins it with the reason `ticket`, unassigning removes nobody, a team
project gets no members; a reassignment leaves holder and stage alone (also through a sync); the
ticket JSON and list_tickets / get_ticket text; the person added by the assignment comes from the
credential; and, with auth off, the same routes still work.

Run: .venv/bin/python tests/test_ticket_assignee.py
"""
import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env

_TMP = tempfile.mkdtemp(prefix="tares-assignee-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-assignee"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import github_tickets as gt
from tares import linear
import tares.mcp_server as m

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── the fake Linear: it sends `assignee` only when the query asks for it ──────
LIN_PROJECT = {"id": "lp-uuid-1", "name": "Invoicing",
               "url": "https://linear.app/acme/project/invoicing-ab12cd34ef56",
               "slugId": "ab12cd34ef56"}
LIN = {"assignees": {}}     # issue id -> the assignee object Linear sends


def lin_issue(n):
    return {"id": f"i{n}", "identifier": f"FAC-{n}", "title": f"Issue {n}",
            "url": f"https://linear.app/acme/issue/FAC-{n}", "sortOrder": float(n),
            "updatedAt": "2026-10-07T10:00:00Z", "state": {"name": "Todo", "type": "unstarted"}}


def lin_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) != linear.API_URL:
        return httpx.Response(404)
    body = json.loads(request.content)
    q, v = body["query"], body.get("variables") or {}
    if "issues(" in q:
        nodes = []
        for n in range(1, 6):
            node = lin_issue(n)
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

# ── the fake GitHub: it sends `assignees` only when the query asks for it ─────
GH = {"assignees": {}}      # issue number -> [logins]
LABEL = "tares:Gh project"


def gh_handler(request: httpx.Request) -> httpx.Response:
    if str(request.url) != gt.API_URL:
        return httpx.Response(404)
    body = json.loads(request.content)
    q = body["query"]
    nodes = []
    for n in range(1, 5):
        node = {"id": f"I_{n}", "number": n, "title": f"Gh issue {n}",
                "url": f"https://github.com/acme/app/issues/{n}", "state": "OPEN",
                "stateReason": None, "labels": {"nodes": [{"name": LABEL}]}, "milestone": None,
                "blockedBy": {"nodes": []}}
        if "assignees(first: 1)" in q:
            # the fake sends every assignee (issue 4 has two), so the first one must be picked
            node["assignees"] = {"nodes": [{"login": x} for x in GH["assignees"].get(n, [])]}
        nodes.append(node)
    return httpx.Response(200, json={"data": {"rateLimit": {"remaining": 4999, "resetAt": "x"},
        "repository": {"nameWithOwner": "acme/app", "url": "https://github.com/acme/app",
                       "milestones": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                       "issues": {"nodes": nodes,
                                  "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})


gt._transport = httpx.MockTransport(gh_handler)


class Catch(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def free_port() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return str(s.getsockname()[1])


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        tp = httpx.ASGITransport(app=app)
        ROOT = {"Authorization": f"Bearer {TOKEN}"}
        cx = httpx.AsyncClient(transport=tp, base_url="http://t", headers=ROOT)

        async def person(name, email, **kw):
            r = await cx.post("/api/people", json={"name": name, "email": email, **kw})
            return r.json()["id"]

        async def key(pid, scope="admin"):
            r = await cx.post("/api/keys", json={"name": f"k-{pid}", "scopes": [scope],
                                                 "person_id": pid})
            return {"Authorization": f"Bearer {r.json()['secret']}"}

        kiran = await person("Kiran", "kiran@x.dev", github="Kiran-GH",
                             emails=["kiran@work.dev"])
        lena = await person("Lena", "lena@x.dev")
        owner = await person("Olga", "olga@x.dev")
        owner_key = await key(owner)
        linear._save_connection(store, {"kind": "api_key", "token": "lin_api_good",
                                        "account": {"name": "Ada"}})
        store.upsert_github_credential("cred-a", "tok-a")

        async def tickets(uid, headers=None):
            r = await cx.get(f"/api/projects/{uid}/tickets", headers=headers or ROOT)
            return {t["number"]: t for t in r.json()["tickets"]}

        # ── Linear ──────────────────────────────────────────────────────────
        print("== Linear: the assignee is mapped to a person ==")
        LIN["assignees"] = {
            "i1": {"email": "KIRAN@x.dev", "name": "Kiran R"},          # main email, other case
            "i2": {"email": "kiran@work.dev", "name": "Kiran R"},       # an extra email
            "i3": {"email": "ghost@x.dev", "name": "Ghost"},            # nobody on the cell
            "i4": {"email": None, "name": "Nameless Nat"},              # a key that reads no emails
        }                                                                # i5 has no assignee
        uid = (await cx.post("/api/projects", json={"template": "custom", "name": "Invoices",
                                                    "objects": []})).json()["id"]
        catch = Catch()
        logging.getLogger("tares.linear").addHandler(catch)
        r = await cx.post(f"/api/projects/{uid}/linear", json={"project": LIN_PROJECT["url"]})
        ck("link answers 200", r.status_code == 200, r.text[:300])
        tk = await tickets(uid)
        ck("owner still says where it lives", all(t["owner"] == "linear" for t in tk.values()),
           [t["owner"] for t in tk.values()])
        ck("by main email, case ignored", tk[1]["assignee"] == {"id": kiran, "name": "Kiran"}
           and tk[1]["assignee_raw"] is None, tk[1])
        ck("by a person's extra email", tk[2]["assignee"] == {"id": kiran, "name": "Kiran"}
           and tk[2]["assignee_raw"] is None, tk[2])
        ck("an email nobody has: unowned, raw is the email", tk[3]["assignee"] is None
           and tk[3]["assignee_raw"] == "ghost@x.dev", tk[3])
        ck("a null email: unowned, raw is the name", tk[4]["assignee"] is None
           and tk[4]["assignee_raw"] == "Nameless Nat", tk[4])
        ck("no assignee: unowned, nothing raw", tk[5]["assignee"] is None
           and tk[5]["assignee_raw"] is None, tk[5])
        ck("the link's sync warned about the null email", any(
            "without an email" in x for x in catch.records), catch.records)
        catch.records.clear()
        LIN["assignees"]["i5"] = {"email": None, "name": "Other Nameless"}
        await linear.sync_project(store, uid)
        ck("a sync with two null emails warns once, not once per ticket",
           len(catch.records) == 1 and "2 assignee(s)" in catch.records[0], catch.records)
        del LIN["assignees"]["i5"]
        catch.records.clear()
        await linear.sync_project(store, uid)
        ck("a sync with one null email warns once", len(catch.records) == 1, catch.records)
        logging.getLogger("tares.linear").removeHandler(catch)

        print("== Linear: later changes ==")
        p_ghost = await person("Ghost", "ghost@x.dev")
        LIN["assignees"]["i1"] = {"email": "lena@x.dev", "name": "Lena"}      # reassigned
        del LIN["assignees"]["i2"]                                            # unassigned
        await linear.sync_project(store, uid)
        tk = await tickets(uid)
        ck("a reassignment in Linear follows", tk[1]["assignee"]["id"] == lena, tk[1])
        ck("an unassignment in Linear follows", tk[2]["assignee"] is None
           and tk[2]["assignee_raw"] is None, tk[2])
        ck("a person added later is picked up by the next sync",
           tk[3]["assignee"] == {"id": p_ghost, "name": "Ghost"} and tk[3]["assignee_raw"] is None,
           tk[3])

        print("== a reassignment leaves a held ticket alone ==")
        t1 = store.get_ticket(uid, "FAC-1")
        store.update_ticket(t1["id"], holder="crew-builder-1", stage="doing")
        LIN["assignees"]["i1"] = {"email": "kiran@x.dev", "name": "Kiran"}
        await linear.sync_project(store, uid)
        t = (await cx.get(f"/api/projects/{uid}/tickets/FAC-1")).json()
        ck("assignee moved, holder and stage did not", t["assignee"]["id"] == kiran
           and t["holder"] == "crew-builder-1" and t["stage"] == "doing", t)

        print("== a Linear ticket does not take an assignee here ==")
        r = await cx.put(f"/api/projects/{uid}/tickets/FAC-1", json={"assignee": lena})
        ck("PUT assignee on a Linear ticket: 409 that says where to change it",
           r.status_code == 409 and "Linear" in r.text, r.text)
        ck("and nothing changed", store.get_ticket(uid, "FAC-1")["assignee_person_id"] == kiran)

        print("== Linear on a private project: assigned people join it ==")
        store.set_project_scope(uid, "private")
        LIN["assignees"]["i2"] = {"email": "lena@x.dev", "name": "Lena"}
        LIN["assignees"]["i5"] = {"email": "olga-not-here@x.dev", "name": "Nobody"}
        await linear.sync_project(store, uid)
        mem = {x["person_id"]: x["reason"] for x in store.list_project_members(uid)}
        ck("Lena (newly assigned FAC-2) is a member for a ticket", mem.get(lena) == "ticket", mem)
        ck("Kiran (assigned before it was private, unchanged) is not added by a later sync",
           kiran not in mem, mem)
        ck("an unknown assignee joins nobody", len(mem) == 1, mem)

        # ── GitHub ──────────────────────────────────────────────────────────
        print("== GitHub: the first assignee is mapped by login ==")
        gid = (await cx.post("/api/projects", json={"template": "custom", "name": "Gh project",
                                                    "objects": []})).json()["id"]
        GH["assignees"] = {1: ["kiran-gh"], 2: ["Octo-Unknown"], 3: [], 4: ["lena-not-gh", "kiran-gh"]}
        r = await cx.post(f"/api/projects/{gid}/github", json={"repo": "acme/app",
                                                              "issue_label": LABEL})
        ck("link answers 200", r.status_code == 200, r.text[:300])
        tk = await tickets(gid)
        ck("owner is github", all(t["owner"] == "github" for t in tk.values()))
        ck("login mapped (case ignored)", tk[1]["assignee"] == {"id": kiran, "name": "Kiran"}
           and tk[1]["assignee_raw"] is None, tk[1])
        ck("unknown login: unowned, raw is the login", tk[2]["assignee"] is None
           and tk[2]["assignee_raw"] == "Octo-Unknown", tk[2])
        ck("no assignee", tk[3]["assignee"] is None and tk[3]["assignee_raw"] is None, tk[3])
        ck("only the first assignee counts", tk[4]["assignee"] is None
           and tk[4]["assignee_raw"] == "lena-not-gh", tk[4])
        store.update_person(lena, github="lena-not-gh")
        GH["assignees"][1] = []
        await gt.sync_project(store, gid)
        tk = await tickets(gid)
        ck("a GitHub login added to a person is picked up; unassigning follows",
           tk[4]["assignee"]["id"] == lena and tk[1]["assignee"] is None, (tk[4], tk[1]))

        # ── Tares tickets ───────────────────────────────────────────────────
        print("== Tares tickets: routes ==")
        team = (await cx.post("/api/projects", json={"template": "custom", "name": "Team work",
                                                     "objects": []})).json()["id"]
        r = await cx.post(f"/api/projects/{team}/tickets", json={"title": "A", "assignee": kiran})
        ck("create with a person id", r.status_code == 201 and r.json()["assignee"] ==
           {"id": kiran, "name": "Kiran"} and r.json()["owner"] == "tares", r.text[:300])
        r = await cx.post(f"/api/projects/{team}/tickets",
                          json={"title": "B", "assignee": "Kiran@Work.dev"})
        ck("create with an extra email", r.json()["assignee"]["id"] == kiran, r.text[:300])
        r = await cx.post(f"/api/projects/{team}/tickets",
                          json={"title": "C", "assignee": "kiran@elsewhere.com"})
        ck("an unknown email is kept as raw, no person", r.status_code == 201
           and r.json()["assignee"] is None and r.json()["assignee_raw"] == "kiran@elsewhere.com",
           r.text[:300])
        c_id = r.json()["id"]
        r = await cx.post(f"/api/projects/{team}/tickets", json={"title": "D", "assignee": "nobody"})
        ck("text that is neither an id nor an email: 404, no ticket made", r.status_code == 404
           and len(await tickets(team)) == 3, r.text)
        r = await cx.put(f"/api/projects/{team}/tickets/{c_id}", json={"assignee": "KIRAN@x.dev"})
        ck("update: an email that now matches a person", r.json()["assignee"]["id"] == kiran
           and r.json()["assignee_raw"] is None, r.text[:300])
        r = await cx.put(f"/api/projects/{team}/tickets/{c_id}", json={"assignee": ""})
        ck("update: empty clears", r.json()["assignee"] is None
           and r.json()["assignee_raw"] is None, r.text[:300])
        r = await cx.put(f"/api/projects/{team}/tickets/{c_id}", json={"title": "C2"})
        ck("an update that does not say assignee leaves it", r.status_code == 200)
        r = await cx.put(f"/api/projects/{team}/tickets/{c_id}", json={"assignee": lena})
        r = await cx.put(f"/api/projects/{team}/tickets/{c_id}", json={"title": "C3"})
        ck("... after an assignment too", r.json()["assignee"]["id"] == lena, r.text[:300])
        ck("a team project gets no members from assignments",
           store.list_project_members(team) == [], store.list_project_members(team))

        print("== Tares tickets: write_ticket and the text tools ==")
        m._cx = lambda timeout=10: httpx.AsyncClient(timeout=timeout, headers=ROOT, transport=tp)
        m.TARESD = "http://t"
        out = json.loads(await m.write_ticket("From MCP", assignee="lena@x.dev", project="Team work"))
        ck("write_ticket: assignee by email", out["assignee"] == {"id": lena, "name": "Lena"}
           and out["assignee_raw"] is None, out)
        mcp_id = out["id"]
        out = json.loads(await m.write_ticket("", id=mcp_id, assignee="kiran@elsewhere.com",
                                              project="Team work"))
        ck("write_ticket: an unknown email is raw", out["assignee"] is None
           and out["assignee_raw"] == "kiran@elsewhere.com", out)
        out = json.loads(await m.list_tickets("Team work"))
        by_title = {t["title"]: t for t in out["tickets"]}
        ck("list_tickets: assignee_note names the person",
           by_title["A"]["assignee_note"] == "assignee: Kiran"
           and by_title["A"]["assignee"] == {"id": kiran, "name": "Kiran"}, by_title["A"])
        ck("list_tickets: unowned with the raw assignee",
           by_title["From MCP"]["assignee_note"] == "unowned (kiran@elsewhere.com is not on this cell)"
           and by_title["From MCP"]["assignee_raw"] == "kiran@elsewhere.com", by_title["From MCP"])
        none = json.loads(await m.write_ticket("Nobody's", project="Team work"))
        out = json.loads(await m.list_tickets("Team work"))
        ck("list_tickets: a ticket with no assignee says unowned",
           next(t for t in out["tickets"] if t["id"] == none["id"])["assignee_note"] == "unowned", out)
        got = json.loads(await m.get_ticket("T1", "Team work"))
        ck("get_ticket: assignee and its note", got["assignee"]["id"] == kiran
           and got["assignee_note"] == "assignee: Kiran", got.get("assignee_note"))
        out = json.loads(await m.list_tickets("Invoices"))
        ck("list_tickets on Linear tickets: the synced assignee shows",
           any(t["assignee_note"] == "unowned (Nameless Nat is not on this cell)"
               for t in out["tickets"]), [t["assignee_note"] for t in out["tickets"]])
        bad = await m.write_ticket("x", assignee="nobody", project="Team work")
        ck("write_ticket: junk assignee comes back as an error", bad.startswith("error 404"), bad)
        bad = await m.write_ticket("", id="FAC-1", assignee=lena, project="Invoices")
        ck("write_ticket: a Linear ticket refuses it", bad.startswith("error 409"), bad)

        print("== private project: assigning adds a member ==")
        m._cx = lambda timeout=10: httpx.AsyncClient(timeout=timeout, headers=owner_key, transport=tp)
        priv = (await cx.post("/api/projects", headers=owner_key,
                              json={"template": "custom", "name": "Secret", "objects": []})).json()["id"]
        r = await cx.patch(f"/api/projects/{priv}", headers=owner_key, json={"scope": "private"})
        ck("project is private, led by Olga", r.json()["scope"] == "private"
           and r.json()["lead"]["id"] == owner, r.text[:200])
        out = json.loads(await m.write_ticket("S1", assignee="kiran@work.dev", project="Secret"))
        mem = {x["person_id"]: x for x in store.list_project_members(priv)}
        ck("assigned person is a member with reason ticket", mem.get(kiran, {}).get("reason") == "ticket",
           mem)
        ck("added by the person behind the credential, not by a header or body",
           mem[kiran]["added_by"] == owner, mem[kiran])
        r = await cx.post(f"/api/projects/{priv}/tickets", headers={**owner_key, "X-Tares-Person": lena},
                          json={"title": "S2", "assignee": lena, "by": lena, "person_id": lena})
        mem = {x["person_id"]: x for x in store.list_project_members(priv)}
        ck("a header or body naming someone else changes nothing", mem[lena]["added_by"] == owner, mem)
        out2 = json.loads(await m.write_ticket("", id=out["id"], assignee="", project="Secret"))
        ck("unassigning removes nobody", out2["assignee"] is None and kiran in
           {x["person_id"] for x in store.list_project_members(priv)})
        json.loads(await m.write_ticket("S3", assignee="stranger@elsewhere.com", project="Secret"))
        ck("an unknown email joins nobody", len(store.list_project_members(priv)) == 2,
           store.list_project_members(priv))
        store.add_project_member(priv, p_ghost, owner, reason="manual")
        json.loads(await m.write_ticket("S4", assignee="ghost@x.dev", project="Secret"))
        ck("a manual member keeps its reason when assigned", {x["person_id"]: x["reason"] for x in
                                          store.list_project_members(priv)}.get(p_ghost) == "manual")

        print("== reassigning a held Tares ticket ==")
        t_a = store.get_ticket(team, "T1")
        store.update_ticket(t_a["id"], holder="crew-builder-2", stage="review")
        r = await cx.put(f"/api/projects/{team}/tickets/{t_a['id']}", json={"assignee": lena})
        j = r.json()
        ck("assignee changed; holder and stage did not; JSON shows both",
           j["assignee"]["id"] == lena and j["holder"] == "crew-builder-2" and j["stage"] == "review",
           j)

    # ── auth off ────────────────────────────────────────────────────────────
    print("== auth off: the routes work as they did, the person is `local` ==")
    home = tempfile.mkdtemp(prefix="assignee-off-")
    port = free_port()
    env = {**clean_env(), "TARES_DB": os.path.join(home, "off.duckdb"), "TARES_PORT": port,
           "TARES_OTLP_GRPC_PORT": "off"}
    proc = subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    B = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(timeout=30) as c2:
            for _ in range(80):
                try:
                    if (await c2.get(f"{B}/health", timeout=1)).status_code == 200:
                        break
                except Exception:
                    await asyncio.sleep(0.25)
            pid = (await c2.post(f"{B}/api/people", json={"name": "Una", "email": "una@x.dev"})).json()["id"]
            uid = (await c2.post(f"{B}/api/projects", json={"template": "custom", "name": "Solo",
                                                           "objects": []})).json()["id"]
            await c2.patch(f"{B}/api/projects/{uid}", json={"scope": "private"})
            r = await c2.post(f"{B}/api/projects/{uid}/tickets",
                              json={"title": "T", "assignee": "una@x.dev"})
            ck("create with an assignee", r.status_code == 201 and r.json()["assignee"]["id"] == pid,
               r.text[:300])
            r = await c2.post(f"{B}/api/projects/{uid}/tickets", json={"title": "U"})
            ck("create without one: unowned, as before", r.status_code == 201
               and r.json()["assignee"] is None and r.json()["assignee_raw"] is None, r.text[:300])
            r = await c2.get(f"{B}/api/projects/{uid}")
            mem = {x["id"]: x["reason"] for x in r.json()["members"]}
            ck("the assigned person joined the private project", mem.get(pid) == "ticket", mem)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
