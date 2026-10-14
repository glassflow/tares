"""A project in one session (TR-520).

The person takes a project back from the crew (`tares factory session <project>`): only the
person, never a station; the project leaves the crew's work list at once and is listed as
releasing, with the stations that hold its tickets, until the orchestrator has stopped its
builders and releases it. Releasing clears the holders and keeps the stages and pull requests. A
later hand-over gives it back to the crew. A project the crew never held is finished in the
person's own words from their session.

Run: .venv/bin/python tests/test_factory_takeover.py
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-takeover-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-takeover"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk

    def as_station(station="", role="", parent=""):
        headers.clear()
        headers.update(root)
        if station:
            headers.update({"X-Tares-Station": station, "X-Tares-Role": role})
        if parent:
            headers["X-Tares-Parent"] = parent

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        notes = json.loads(await m.create_project("Notes", "A small notes app"))["id"]
        for t in ("Catalog", "Payments"):
            await m.write_ticket(t, project="Shop")
        await m.write_ticket("Save a note", project="Notes")

        print("== nothing to take over ==")
        async with mk() as cx:
            r = await cx.post(f"/api/projects/{shop}/takeover", json={"words": "mine now"})
        ck("a project the crew does not hold cannot be taken over", r.status_code == 409
           and "not with the crew" in r.text, r.text)

        async with mk() as cx:
            await cx.post(f"/api/projects/{shop}/handover",
                          json={"repos": [{"path": "/w/shop", "name": "acme/shop"}]})
        as_station("crew-orchestrator", "orchestrator")
        await mf.assign_ticket("T1", "shop-build-m1", project="Shop")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        async with mk() as cx:
            await cx.post(f"/api/projects/{shop}/tickets/T1/stage",
                          json={"stage": "review", "pr": "https://github.com/acme/shop/pull/7"})

        print("== only the person takes it over ==")
        for st, role in (("shop-build-m1", "builder"), ("crew-orchestrator", "orchestrator")):
            as_station(st, role)
            async with mk() as cx:
                r = await cx.post(f"/api/projects/{shop}/takeover", json={"words": "mine now"})
            ck(f"the {role} may not", r.status_code == 403 and "the person" in r.text, r.text)
        as_station()
        async with mk() as cx:
            r = await cx.post(f"/api/projects/{shop}/takeover",
                              json={"words": "small enough, I will finish it myself"})
            ps = (await cx.get("/api/crew/projects")).json()
        ck("the person's own terminal may, in their words", r.status_code == 200
           and r.json()["takeover"]["words"] == "small enough, I will finish it myself"
           and r.json()["takeover"]["released_at"] is None, r.text)
        ck("it says who holds its tickets", r.json().get("holders") == ["shop-build-m1"], r.json())
        ck("off the crew's work list at once", "Shop" not in [p["name"] for p in ps["projects"]],
           ps["projects"])
        rel = ps.get("releasing") or []
        ck("listed as releasing, with the holders", [p["name"] for p in rel] == ["Shop"]
           and rel[0]["holders"] == ["shop-build-m1"] and rel[0]["repos"][0]["name"] == "acme/shop",
           rel)
        mcp = json.loads(await mf.crew_projects())
        ck("crew_projects over MCP shows it too", [p["name"] for p in mcp.get("releasing") or []]
           == ["Shop"], mcp)
        async with mk() as cx:
            again = await cx.post(f"/api/projects/{shop}/takeover", json={"words": "again"})
        ck("asking again changes nothing", again.status_code == 200
           and again.json()["takeover"]["words"] == "small enough, I will finish it myself",
           again.text)

        print("== the orchestrator releases it ==")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        bad = await mf.release_project("Shop")
        ck("a builder may not release", "only the orchestrator" in bad, bad)
        as_station("crew-orchestrator", "orchestrator")
        out = json.loads(await mf.release_project("Shop"))
        ck("the orchestrator releases it", out["takeover"]["released_at"] is not None, out)
        t1 = json.loads(await m.get_ticket("T1", project="Shop"))
        ck("the holder is cleared, the stage and PR stay", t1.get("holder") in (None, "")
           and t1.get("stage") == "review" and "pull/7" in json.dumps(t1.get("pr")), t1)
        async with mk() as cx:
            ps = (await cx.get("/api/crew/projects")).json()
            hist = (await cx.get(f"/api/projects/{shop}/tickets/T1")).json()
        ck("no longer releasing", not ps.get("releasing"), ps.get("releasing"))
        ck("the release is in the ticket's history", "released to the person's session"
           in json.dumps(hist), json.dumps(hist)[-400:])
        again = json.loads(await mf.release_project("Shop"))
        ck("releasing twice is harmless", again["takeover"]["released_at"]
           == out["takeover"]["released_at"], again)

        print("== handing it back ==")
        as_station()
        async with mk() as cx:
            await cx.post(f"/api/projects/{shop}/handover",
                          json={"repos": [{"path": "/w/shop", "name": "acme/shop"}]})
            ps = (await cx.get("/api/crew/projects")).json()
            ho = (await cx.get(f"/api/projects/{shop}/crew")).json()["handover"]
        ck("a new hand-over gives it back to the crew", "Shop" in [p["name"] for p in ps["projects"]]
           and ho["takeover"] is None, (ps, ho))

        print("== a project the crew never held, finished from the person's session ==")
        bad = await mf.finish_project("Notes", words="done, it works")
        ck("not while a ticket is open", "still has open tickets: T1" in bad, bad)
        await m.write_ticket("Save a note", id="T1", status="done", project="Notes")
        as_station("crew-orchestrator", "orchestrator")
        bad = await mf.finish_project("Notes", "D1")
        ck("a station cannot finish a project the crew never held", "never handed to the crew"
           in bad, bad)
        as_station()
        out = json.loads(await mf.finish_project("Notes", words="done, it works"))
        ck("the person's session finishes it in their words", out["finished"]["words"]
           == "done, it works", out)
        async with mk() as cx:
            pl = (await cx.get("/api/projects")).json()["projects"]
        ck("the project is done", next(p for p in pl if p["id"] == notes)["status"] == "done", pl)
        bad = await mf.finish_project("Notes", words="again")
        ck("finishing twice is refused", "already finished" in bad, bad)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
