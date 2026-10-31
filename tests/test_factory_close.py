"""Ready to close (TR-731): a software factory project that is still open and whose tickets are
all merged or otherwise finished says `ready_to_close` on its card in GET /api/projects and in its
summary, so the console can offer to close it. POST /api/projects/{id}/finish closes it.

Run: .venv/bin/python tests/test_factory_close.py
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-close-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-close"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    m.TARESD = "http://t"
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t",
                                              headers={"Authorization": f"Bearer {TOKEN}"})
    m._cx = mk

    async def card(cx, uid):
        pl = (await cx.get("/api/projects")).json()["projects"]
        return next(p for p in pl if p["id"] == uid)

    async def summary(cx, uid):
        return (await cx.get(f"/api/projects/{uid}/summary")).json()

    async with app.router.lifespan_context(app):
        ids = {}
        for name in ("project-x", "project-w", "project-v", "project-u"):
            ids[name] = json.loads(await m.create_project(name, "A small test project"))["id"]
        # project-x: three merged tickets (one by status, two by stage)
        await m.write_ticket("Alpha", status="done", project="project-x")
        for t in ("Beta", "Gamma"):
            await m.write_ticket(t, project="project-x")
        async with mk() as cx:
            for n in ("T2", "T3"):
                r = await cx.post(f"/api/projects/{ids['project-x']}/tickets/{n}/stage",
                                  json={"stage": "merged"})
                ck(f"{n} is marked merged", r.status_code == 200, r.text)
        # project-w: one merged, one in review
        await m.write_ticket("Alpha", status="done", project="project-w")
        await m.write_ticket("Beta", project="project-w")
        # project-v: only canceled tickets
        for t in ("Alpha", "Beta"):
            await m.write_ticket(t, status="canceled", project="project-v")
        # project-u: no tickets at all
        # a project that is not a software factory project
        async with mk() as cx:
            r = await cx.post("/api/projects", json={"template": "custom", "name": "plain-project",
                                                      "objects": []})
            plain = r.json()["id"]
            ck("the plain project is made", r.status_code == 201, r.text)

            print("== the rule ==")
            ck("three merged tickets: ready on the list", (await card(cx, ids["project-x"]))
               .get("ready_to_close") is True, await card(cx, ids["project-x"]))
            ck("three merged tickets: ready in the summary", (await summary(cx, ids["project-x"]))
               .get("ready_to_close") is True)

            r = await cx.post(f"/api/projects/{ids['project-w']}/tickets/T2/stage",
                              json={"stage": "review", "pr": "https://github.com/acme/w/pull/3"})
            ck("project-w's second ticket is in review", r.status_code == 200, r.text)
            ck("a ticket in review: not ready on the list", (await card(cx, ids["project-w"]))
               .get("ready_to_close") is False)
            ck("a ticket in review: not ready in the summary", (await summary(cx, ids["project-w"]))
               .get("ready_to_close") is False)

            ck("only canceled tickets: not ready on the list", (await card(cx, ids["project-v"]))
               .get("ready_to_close") is False)
            ck("only canceled tickets: not ready in the summary",
               (await summary(cx, ids["project-v"])).get("ready_to_close") is False)

            ck("no tickets: not ready", (await card(cx, ids["project-u"]))
               .get("ready_to_close") is False)

            # finished tickets on a project that is not a software factory project: only its kind
            # keeps it from offering to close
            await m.write_ticket("Alpha", status="done", project=plain)
            ck("a plain project: false on the list", (await card(cx, plain))
               .get("ready_to_close") is False, await card(cx, plain))
            ck("a plain project: false in the summary", (await summary(cx, plain))
               .get("ready_to_close") is False)

            print("== closing ==")
            r = await cx.post(f"/api/projects/{ids['project-w']}/finish", json={"words": "done"})
            ck("finishing with a ticket in review is refused", r.status_code == 409
               and "still has open tickets" in r.text, f"{r.status_code} {r.text}")
            ck("and the project stays open", (await card(cx, ids["project-w"]))["status"]
               != "done")

            r = await cx.post(f"/api/projects/{ids['project-x']}/finish",
                              json={"words": "all merged, closing it"})
            ck("finishing a merged project works", r.status_code == 200
               and r.json()["finished"]["words"] == "all merged, closing it", r.text)
            c = await card(cx, ids["project-x"])
            ck("it is done and no longer ready to close", c["status"] == "done"
               and c["ready_to_close"] is False, c)
            ck("the summary agrees", (await summary(cx, ids["project-x"])).get("ready_to_close")
               is False)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
