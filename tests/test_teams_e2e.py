"""Two people, two crews, one cell: the team flow end to end over HTTP and MCP (TR-638).

A daemon with auth on, two people with their own keys, no real Claude session: recordings go in
through the same ingest path the plugin uses. In order: a private project and its areas; what the
second person cannot see; tickets that make them a member; a hand-over from the first person's
crew; each person's my_work; a merge that frees the second person's ticket; a desk question that
only the person it is addressed to can answer; two crews of the same name on /api/crew.

Run: .venv/bin/python tests/test_teams_e2e.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-teams-e2e-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-teams-e2e"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(seconds: float) -> str:
    t = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def nd(lines) -> str:
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def says(sid, text, seconds):
    return {"type": "user", "sessionId": sid, "cwd": "/w", "timestamp": ts(seconds),
            "message": {"role": "user", "content": text}}


def station(sid, crew, machine):
    return {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(-30),
            "station": "crew-orchestrator", "role": "orchestrator", "parent": "",
            "factory_station": "crew-orchestrator", "factory_crew": crew, "crew": crew,
            "factory_machine": machine, "machine": machine}


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk
    factory.MATCH_WAIT_SECONDS = 0   # an answer that is not recorded yet fails at once

    def as_(h):
        headers.clear()
        headers.update(h)

    async with app.router.lifespan_context(app):
        print("== a cell with auth on, and two people with a key each ==")
        keys, pid = {}, {}
        async with mk() as cx:
            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            ck("the claude_code source is made", r.status_code < 300, r.text)
            for who, mail in (("ashish", "ashish@example.com"), ("kiran", "kiran-test@example.com")):
                r = await cx.post("/api/keys", json={"name": f"{who}-laptop", "scopes": ["admin"],
                                                     "person": {"email": mail, "name": who}})
                keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
                pid[who] = r.json()["person"]["id"]
            ck("two people, each with a bound key", len(set(pid.values())) == 2, pid)
            r = await cx.get("/api/projects")
            ck("no key at all is refused", (await httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t").get("/api/projects")
                ).status_code == 401)

        async def push(who, lines):
            async with mk() as cx:
                r = await cx.post("/ingest/claude_code", content=nd(lines),
                                  headers={"Content-Type": "application/x-ndjson", **keys[who]})
                ck(f"{who}'s lines are taken in", r.status_code < 300, r.text)

        async def get(who, path, **params):
            async with mk() as cx:
                return await cx.get(path, params=params, headers=keys[who])

        async def post(who, path, body=None, **hdr):
            async with mk() as cx:
                return await cx.post(path, json=body or {}, headers={**keys[who], **hdr})

        print("== ashish makes a private project and gives each area to someone ==")
        r = await post("ashish", "/api/projects", {"template": "custom", "name": "teams-e2e",
                                                   "objects": [], "kind": "software_factory"})
        ck("the project is made", r.status_code in (200, 201), r.text)
        uid = r.json()["id"]
        async with mk() as cx:
            r = await cx.patch(f"/api/projects/{uid}", json={"scope": "private"}, headers=keys["ashish"])
        ck("it is private", r.status_code == 200 and r.json()["scope"] == "private", r.text)
        for area, who in (("acme/api", "ashish"), ("acme/web", "kiran")):
            async with mk() as cx:
                r = await cx.put(f"/api/areas/{area}", json={"person_id": pid[who]}, headers=keys["ashish"])
            ck(f"{area} is {who}'s", r.status_code < 300, r.text)

        print("== kiran cannot see a private project they are not on ==")
        r = await get("kiran", f"/api/projects/{uid}")
        ck("GET the project: 404", r.status_code == 404, r.text)
        words = "zebra-ledger-7731"
        await push("ashish", [say for say in (
            {"type": "session_station", "sessionId": "e2e-a1", "cwd": "/w", "timestamp": ts(-60),
             "station": "e2e-build", "role": "builder", "parent": ""},
            says("e2e-a1", f"teams-e2e: the {words} is ready", -50))])
        store.link_session(uid, "e2e-a1")
        async with mk() as cx:
            body = {"selector": {"session": "e2e-a1"}, "window": "1h", "include_payload": True}
            mine = (await cx.post("/read", json=body, headers=keys["ashish"])).json()
            theirs = (await cx.post("/read", json=body, headers=keys["kiran"])).json()
        ck("set up: ashish reads the event", mine.get("count", 0) >= 1 and words in json.dumps(mine), mine)
        ck("kiran's read of the session source shows no event of teams-e2e",
           theirs.get("count", 0) == 0 and words not in json.dumps(theirs), theirs)

        print("== tickets across two repos make kiran a member ==")
        r = await post("ashish", f"/api/projects/{uid}/tickets",
                       {"title": "T-a: the api", "repo": "acme/api", "assignee": pid["ashish"]})
        ck("T-a is made", r.status_code == 201, r.text)
        ta = factory.label(r.json())
        ck("T-a is not kiran's: kiran still gets 404", (await get("kiran", f"/api/projects/{uid}")
                                                        ).status_code == 404)
        r = await post("ashish", f"/api/projects/{uid}/tickets",
                       {"title": "T-b: the web", "repo": "acme/web", "assignee": pid["kiran"],
                        "depends_on": [ta]})
        ck("T-b is made, assigned to kiran, after T-a", r.status_code == 201, r.text)
        tb = factory.label(r.json())
        ck("kiran is a member by ticket", store.is_project_member(uid, pid["kiran"])
           and any(x.get("reason") == "ticket" for x in store.list_project_members(uid)),
           store.list_project_members(uid))
        r = await get("kiran", f"/api/projects/{uid}")
        ck("kiran's GET now answers 200", r.status_code == 200, r.text)

        print("== ashish hands the project over from crew a ==")
        r = await post("ashish", f"/api/projects/{uid}/handover",
                       {"repos": [{"path": os.path.join(_TMP, "api"), "name": "acme/api"},
                                  {"path": os.path.join(_TMP, "web"), "name": "acme/web"}]},
                       **{"X-Tares-Crew": "a"})
        ho = (r.json() or {}).get("handover") or {}
        ck("handed over by ashish, filed under crew a", r.status_code == 200
           and ho.get("crew") == "a" and (ho.get("handed_by_person") or {}).get("id") == pid["ashish"]
           and [x["name"] for x in ho.get("repos") or []] == ["acme/api", "acme/web"], r.text)

        async def ready(who, crew):
            r = await get(who, "/api/crew/my-work", crew=crew)
            ck(f"{who}'s my_work answers", r.status_code == 200, r.text)
            return [x["ticket"] for x in r.json()["ready"] if x["project"] == "teams-e2e"]

        print("== my work: each person gets their own ready tickets ==")
        ck("kiran (crew b): nothing is ready, T-b waits on T-a", await ready("kiran", "b") == [])
        ck("ashish (crew a): T-a is ready", await ready("ashish", "a") == [ta])

        print("== T-a merges: T-b becomes kiran's ==")
        r = await post("ashish", f"/api/projects/{uid}/tickets/{ta}/stage", {"stage": "merged"})
        ck("ashish moves T-a to merged", r.status_code == 200, r.text)
        ck("kiran: T-b is ready", await ready("kiran", "b") == [tb])
        ck("ashish: nothing is ready now", await ready("ashish", "a") == [])

        print("== a question about T-b goes to kiran, and only kiran's words answer it ==")
        as_({**keys["ashish"], "X-Tares-Station": "crew-orchestrator", "X-Tares-Role": "orchestrator"})
        out = await mf.desk_add("teams-e2e: web in one page or two?", "one", "it is smaller",
                                project="teams-e2e", tickets=[tb])
        d = json.loads(out)
        ck("the item goes to kiran by default", (d.get("to_person") or {}).get("id") == pid["kiran"], out)
        answer = "One page, and keep it small"
        await push("ashish", [says("e2e-a1", answer, 2)])
        out = await mf.desk_answer(d["label"], answer)
        ck("ashish's own recording does not answer kiran's item", isinstance(out, str)
           and out.startswith("error 409") and "kiran" in out, out)
        ck("the item is still open", store.get_desk_item(d["label"])["state"] == "open")
        await push("kiran", [{"type": "session_station", "sessionId": "e2e-k1", "cwd": "/w",
                              "timestamp": ts(-20), "station": "e2e-web", "role": "builder",
                              "parent": ""}, says("e2e-k1", answer, 3)])
        out = json.loads(await mf.desk_answer(d["label"], answer))
        ck("kiran's recording answers it", out.get("recorded") is True, out)
        dec = next((x for x in store.list_decisions(None) if x["question"].startswith("teams-e2e: web")), {})
        ck("the decision shows kiran", (dec.get("person") or {}).get("id") == pid["kiran"], dec)

        print("== /api/crew shows the two crews, each with its own person ==")
        await push("ashish", [station("e2e-o-a", "a", "ashish-mac")])
        await push("kiran", [station("e2e-o-b", "b", "kiran-pc")])
        r = await get("ashish", "/api/crew")
        crews = r.json().get("crews") or []
        by = {c["name"]: c for c in crews}
        ck("crews a and b are both there", {"a", "b"} <= set(by), [c["name"] for c in crews])
        ck("with different people", by["a"]["person"]["id"] == pid["ashish"]
           and by["b"]["person"]["id"] == pid["kiran"], crews)
        ck("and their own machines", (by["a"]["machine"], by["b"]["machine"]) == ("ashish-mac", "kiran-pc"),
           crews)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
