"""Docs and tickets (TR-403): what a spec session leaves in a project for the session that builds
it.

Covers validation, the REST routes for docs (CRUD, a doc shared by two projects, the size limit,
admin-only writes, a project key reading its own project's docs) and for tickets (Tares-owned
create and edit, Linear-owned rows refusing edits, the working doc link, lookup by Linear
identifier), and that a project update or delete treats docs and tickets right.

Run: .venv/bin/python tests/test_docs.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tares-docs-")
DB = os.path.join(_TMP, "api.duckdb")
TOKEN = "root-token-docs"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import docs

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def raises(fn, *a):
    try:
        fn(*a)
    except docs.DocError as e:
        return str(e)
    return None


def unit():
    print("== validation ==")
    ck("a known kind passes", docs.validate_doc("Spec", " The  spec ", "# x") ==
       ("spec", "The spec", "# x"))
    ck("unknown kind names the kinds", "spec" in (raises(docs.validate_doc, "memo", "t", "") or ""))
    ck("empty title refused", raises(docs.validate_doc, "spec", "  ", "") is not None)
    ck("long title refused", raises(docs.validate_doc, "spec", "x" * 201, "") is not None)
    ck("body over 256 KB refused",
       raises(docs.validate_doc, "spec", "t", "x" * (256 * 1024 + 1)) is not None)
    ck("status normalized", docs.status_ok("In Progress") == "in_progress")
    ck("unknown status refused", raises(docs.status_ok, "blocked") is not None)
    ck("no em dash in messages", "—" not in (raises(docs.validate_doc, "memo", "t", "") or ""))


async def api():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=root)
        r = await cx.post("/api/keys", json={"name": "reader", "scopes": ["read"]})
        reader = {"Authorization": f"Bearer {r.json()['secret']}"}
        p1 = (await cx.post("/api/projects", json={"template": "custom", "name": "Invoicing",
                                                   "objects": []})).json()["id"]
        p2 = (await cx.post("/api/projects", json={"template": "custom", "name": "Billing",
                                                   "objects": []})).json()["id"]
        base = f"/api/projects/{p1}/docs"

        print("== docs ==")
        r = await cx.post(base, json={"kind": "spec", "title": "Spec", "body": "# What\n\nbuild",
                                      "by": "spec session"})
        ck("create -> 201 with id, body and who wrote it", r.status_code == 201
           and r.json()["id"].startswith("doc_") and r.json()["body"] == "# What\n\nbuild"
           and r.json()["updated_by"] == "spec session", r.text)
        spec = r.json()["id"]
        r = await cx.post(base, json={"kind": "spec", "title": "Spec", "body": "again"})
        ck("same title again is fine: ids, not names", r.status_code == 201, r.text)
        await cx.delete(f"{base}/{r.json()['id']}")
        r = await cx.post(base, json={"kind": "memo", "title": "x", "body": ""})
        ck("unknown kind -> 400", r.status_code == 400, r.text)
        r = await cx.post(base, json={"kind": "spec", "title": "x", "body": "x" * 300000})
        ck("too big -> 400", r.status_code == 400 and "KB" in r.json()["detail"], r.text)
        r = await cx.post("/api/projects/uc_nope/docs", json={"kind": "spec", "title": "x"})
        ck("unknown project -> 404", r.status_code == 404, r.text)
        for kind, title in (("plan", "Plan"), ("agents", "AGENTS.md"), ("start", "Start here"),
                            ("working", "Working: parser")):
            await cx.post(base, json={"kind": kind, "title": title, "body": f"{kind} body"})
        r = await cx.get(base)
        own = [d for d in r.json() if not d["global"]]
        ck("list in reading order, no bodies, sizes",
           [d["kind"] for d in own] == ["start", "spec", "plan", "agents", "working"]
           and "body" not in own[0] and own[1]["size"] == len("# What\n\nbuild"), r.text)

        print("== global docs ==")
        shared = [d for d in r.json() if d["global"]]
        ck("the first project with docs gets the shared AGENTS.md and memory",
           sorted((d["kind"], d["title"]) for d in shared)
           == [("agents", "AGENTS.md (all projects)"), ("memory", "Memory (all projects)")], shared)
        g = (await cx.get("/api/docs/global")).json()
        ck("readable on the cell", g["agents"]["global"] and "every project" in g["agents"]["body"],
           g)
        r = await cx.post("/api/docs/global/agents/lines",
                          json={"text": "Always build on a branch, never commit to main.",
                                "by": "spec session"})
        ck("add a line", r.status_code == 200 and r.json()["added"]
           and r.json()["body"].endswith("- Always build on a branch, never commit to main.\n")
           and r.json()["updated_by"] == "spec session", r.text)
        r = await cx.post("/api/docs/global/agents/lines",
                          json={"text": "Always build on a branch, never commit to main."})
        ck("the same line again is not added twice", r.json()["added"] is False
           and r.json()["body"].count("Always build on a branch") == 1, r.text)
        r = await cx.post("/api/docs/global/memory/lines", json={"text": "  "})
        ck("an empty line -> 400", r.status_code == 400, r.text)
        r = await cx.post("/api/docs/global/notes/lines", json={"text": "x"})
        ck("an unknown shared doc -> 404", r.status_code == 404, r.text)
        r = await cx.post("/api/docs/global/memory/lines", json={"text": "x"}, headers=reader)
        ck("a read key cannot add -> 403", r.status_code == 403, r.text)
        r = await cx.delete(f"{base}/{g['agents']['id']}")
        ck("a project cannot drop a shared doc -> 409", r.status_code == 409, r.text)
        ck("a project without docs does not get them yet",
           (await cx.get(f"/api/projects/{p2}/docs")).json() == [])
        r = await cx.get(base, params={"kind": "working"})
        ck("filter by kind", [d["kind"] for d in r.json()] == ["working"], r.text)
        r = await cx.put(f"{base}/{spec}", json={"body": "# What\n\nbuild v2"})
        ck("update body keeps title, by falls back to the credential",
           r.status_code == 200 and r.json()["title"] == "Spec"
           and r.json()["body"].endswith("v2") and r.json()["updated_by"] == "auth token (env)",
           r.text)
        r = await cx.get(f"/api/projects/{p2}/docs/{spec}")
        ck("another project does not see it", r.status_code == 404, r.text)

        print("== shared docs ==")
        r = await cx.post(f"/api/projects/{p2}/docs/{spec}/use")
        ck("include in a second project", r.status_code == 200 and sorted(r.json()["projects"])
           == sorted([p1, p2]), r.text)
        ck("which brings the shared docs with it",
           sum(d["global"] for d in (await cx.get(f"/api/projects/{p2}/docs")).json()) == 2)
        await cx.put(f"/api/projects/{p2}/docs/{spec}", json={"body": "shared edit"})
        r = await cx.get(f"{base}/{spec}")
        ck("an edit shows in both", r.json()["body"] == "shared edit", r.text)
        r = await cx.delete(f"/api/projects/{p2}/docs/{spec}")
        ck("removing from one keeps it for the other", r.json()["kept_for"] == [p1]
           and (await cx.get(f"{base}/{spec}")).status_code == 200, r.text)
        r = await cx.get("/api/docs")
        row = next(d for d in r.json() if d["id"] == spec)
        ck("cell list names the projects", row["projects"] == [{"id": p1, "name": "Invoicing"}],
           r.text)
        r = await cx.post(f"/api/projects/{p2}/docs/doc_nope/use")
        ck("using an unknown doc -> 404", r.status_code == 404, r.text)

        print("== auth ==")
        r = await cx.get(base, headers=reader)
        ck("a read key reads docs", r.status_code == 200, r.text)
        r = await cx.post(base, json={"kind": "note", "title": "n"}, headers=reader)
        ck("a read key cannot write -> 403", r.status_code == 403, r.text)
        r = await cx.post(f"/api/projects/{p1}/keys", json={"name": "proj"})
        pk = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await cx.get(base, headers=pk)
        ck("a project key reads its own docs", r.status_code == 200, r.text)
        r = await cx.get(f"/api/projects/{p2}/docs", headers=pk)
        ck("but not another project's", r.status_code == 403, r.text)
        r = await cx.get(f"/api/projects/{p1}/tickets", headers=pk)
        ck("a project key reads its own tickets", r.status_code == 200, r.text)

        print("== tickets (Tares owns them) ==")
        tb = f"/api/projects/{p1}/tickets"
        working = next(d["id"] for d in (await cx.get(base)).json() if d["kind"] == "working")
        r = await cx.post(tb, json={"title": "Parse invoices", "working_doc": working})
        ck("create -> 201 at position 1 with the working doc's title",
           r.status_code == 201 and r.json()["position"] == 1 and r.json()["status"] == "todo"
           and r.json()["owner"] == "tares" and r.json()["working_doc_title"] == "Working: parser",
           r.text)
        t1 = r.json()["id"]
        r = await cx.post(tb, json={"title": "Store invoices", "status": "In progress"})
        ck("second goes to the end, status normalized", r.json()["position"] == 2
           and r.json()["status"] == "in_progress", r.text)
        t2 = r.json()["id"]
        r = await cx.post(tb, json={"title": "x", "working_doc": "doc_nope"})
        ck("unknown working doc -> 404", r.status_code == 404, r.text)
        r = await cx.post(tb, json={"title": "x", "status": "blocked"})
        ck("unknown status -> 400", r.status_code == 400, r.text)
        r = await cx.put(f"{tb}/{t2}", json={"position": 0.5, "status": "done"})
        ck("reorder and status", r.status_code == 200 and r.json()["status"] == "done", r.text)
        r = await cx.get(tb)
        ck("list in order", [t["id"] for t in r.json()["tickets"]] == [t2, t1]
           and r.json()["linear"] is None, r.text)
        r = await cx.get(f"{tb}/{t1}")
        ck("one ticket carries its working doc's body", r.json()["working_doc_body"]
           == "working body", r.text)
        r = await cx.put(f"{tb}/{t1}", json={"working_doc": None})
        ck("unlink the working doc", r.json()["working_doc"] is None, r.text)
        r = await cx.delete(f"{tb}/{t2}")
        ck("delete a Tares ticket", r.status_code == 200 and len(
            (await cx.get(tb)).json()["tickets"]) == 1, r.text)

        print("== tickets (Linear owns them) ==")
        store.set_project_linear(p2, {"id": "lp1", "name": "Billing", "url": "https://l/p"})
        lt = store.create_ticket(p2, "linear", "Charge cards", "todo", 1, external_id="iss-1",
                                 identifier="ENG-7", url="https://l/ENG-7")
        tb2 = f"/api/projects/{p2}/tickets"
        r = await cx.post(tb2, json={"title": "x"})
        ck("no Tares tickets on a Linear project -> 409", r.status_code == 409
           and "Linear" in r.json()["detail"], r.text)
        r = await cx.get(f"{tb2}/eng-7")
        ck("look up by Linear identifier, any case", r.status_code == 200
           and r.json()["id"] == lt, r.text)
        r = await cx.put(f"{tb2}/ENG-7", json={"status": "done"})
        ck("its status changes in Linear -> 409", r.status_code == 409
           and "ENG-7" in r.json()["detail"], r.text)
        w2 = (await cx.post(f"/api/projects/{p2}/docs", json={"kind": "working",
                                                              "title": "Working: cards",
                                                              "body": "steps"})).json()["id"]
        r = await cx.put(f"{tb2}/ENG-7", json={"working_doc": w2})
        ck("but it takes a working doc", r.status_code == 200
           and r.json()["working_doc_body"] == "steps", r.text)
        r = await cx.delete(f"{tb2}/ENG-7")
        ck("cannot delete a Linear ticket -> 409", r.status_code == 409, r.text)
        r = await cx.get(tb2)
        ck("the list says where they live", r.json()["linear"]["id"] == "lp1", r.text)

        print("== project update and delete ==")
        r = await cx.put(f"/api/projects/{p1}", json={"objects": []})
        ck("updating a custom project's objects keeps its docs", r.status_code == 200
           and len([d for d in (await cx.get(base)).json() if not d["global"]]) == 5, r.text)
        await cx.delete(f"{base}/{working}")
        r = await cx.get(f"{tb}/{t1}")
        ck("deleting a working doc unlinks its ticket", r.json()["working_doc"] is None, r.text)
        await cx.post(f"/api/projects/{p1}/docs/{w2}/use")
        r = await cx.delete(f"/api/projects/{p2}")
        ck("delete project -> ok", r.status_code == 200, r.text)
        ck("its tickets go", store.list_tickets(p2) == [])
        ck("a doc another project includes stays", store.get_doc(None, w2) is not None)
        await cx.delete(f"/api/projects/{p1}")
        left = store.list_cell_docs()
        ck("the last project's docs go with it; the shared ones stay",
           sorted(d["kind"] for d in left) == ["agents", "memory"], left)
        await cx.aclose()


def upgrade():
    """A cell from before project kinds: a project with docs, tickets and no agent becomes a
    software factory project, once; an ordinary project stays as it is."""
    print("== project kind upgrade ==")
    from tares.store import Store
    path = os.path.join(_TMP, "upgrade.duckdb")
    s = Store(path)
    s.create_project("uc_spec", "custom", "Spec made", {"objects": []})
    s.create_project("uc_plain", "custom", "Plain", {"objects": []})
    s.create_doc("uc_spec", "spec", "Spec", "x")
    s.create_ticket("uc_spec", "tares", "First")
    s.con.execute("DELETE FROM settings WHERE key = 'factory_kind_filled'")
    s.con.close()
    s = Store(path)
    ck("the spec-made project is a software factory project",
       s.get_project("uc_spec")["kind"] == "software_factory")
    ck("the plain one is not", s.get_project("uc_plain")["kind"] is None)
    s.set_project_kind("uc_spec", None)
    s.con.close()
    s = Store(path)
    ck("it runs once: a kind cleared later stays cleared", s.get_project("uc_spec")["kind"] is None)
    s.con.close()


def main():
    unit()
    upgrade()
    asyncio.run(api())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    main()
