"""An orchestrator asks Tares for "my work" and gets only what belongs to its person (TR-633).

Covers: `GET /api/crew/my-work` (and the `my_work` tool) listing, for the key's person, the tickets
assigned to them that are ready, the ones a station holds, and the milestones of the projects they
lead whose tickets are all delivered; a ticket that waits on someone else's turns ready once that
one is merged; a person with no tickets sees empty lists; `desk` and `spec_questions` are always
there; the route is not taken for a station called `my-work`; a key with no person is refused (also
for hand_over); the root token is the person `local` and keeps working; the crew name comes from
`crew`, the X-Tares-Crew header or FACTORY_CREW, else `crew`. The hand-over records who handed it
over and to which crew; the `hand_over` tool names `<crew>-orchestrator` of the caller's own crew
and never another person's running one; /api/crew/projects narrows to the person's projects and
the root token still sees them all.

Run: .venv/bin/python tests/test_my_work.py
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

_TMP = tempfile.mkdtemp(prefix="tares-mywork-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-mywork"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0
NO_PERSON = "This key has no person. An admin binds it in Settings, Keys."


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def orch_lines(sid, station, crew, machine):
    base = {"sessionId": sid, "cwd": "/w/crew", "factory_station": station}
    return [{"type": "session_station", "timestamp": ts(30), "station": station,
             "role": "orchestrator", "parent": "", "factory_crew": crew, "crew": crew,
             "factory_machine": machine, "machine": machine, **base},
            {"type": "assistant", "timestamp": ts(1), "message": {"role": "assistant",
             "content": "ready"}, **base}]


def nd(lines) -> str:
    return "\n".join(json.dumps(x) for x in lines) + "\n"


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

    def as_(h):
        headers.clear()
        headers.update(h)

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            ck("the claude_code source is made", r.status_code < 300, r.text)
            keys, pid = {}, {}
            for who in ("P", "Q", "R"):
                r = await cx.post("/api/keys", json={
                    "name": f"{who}-laptop", "scopes": ["admin"],
                    "person": {"email": f"{who.lower()}@example.com", "name": who}})
                keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
                pid[who] = r.json()["person"]["id"]
            r = await cx.post("/api/keys", json={"name": "bot", "scopes": ["admin"]})
            keys["bot"] = {"Authorization": f"Bearer {r.json()['secret']}"}
            ck("three people with bound keys and one key with no person", len(set(pid.values())) == 3
               and r.status_code < 300, r.text)

            async def make_project(name):
                r = await cx.post("/api/projects", json={"template": "custom", "name": name,
                                                         "objects": [], "kind": "software_factory"})
                return r.json()["id"]

            async def make_ticket(uid, title, who=None, deps=None, milestone=None):
                body = {"title": title}
                if who:
                    body["assignee"] = pid[who]
                if deps:
                    body["depends_on"] = deps
                if milestone:
                    body["milestone"] = milestone
                r = await cx.post(f"/api/projects/{uid}/tickets", json=body)
                assert r.status_code == 201, r.text
                return r.json()

            shop = await make_project("Shop")
            t1 = await make_ticket(shop, "P first", "P")
            t2 = await make_ticket(shop, "P second", "P")
            t3 = await make_ticket(shop, "Q after P", "Q", deps=[t1["id"]])
            t4 = await make_ticket(shop, "Q second", "Q")
            blog = await make_project("Blog")       # R leads it; one of Q's tickets is delivered
            r = await cx.post(f"/api/projects/{blog}/milestones", json={"name": "M1"})
            ms = r.json()["id"]
            r = await cx.post(f"/api/projects/{blog}/milestones", json={"name": "M2"})
            ms2 = r.json()["id"]
            b1 = await make_ticket(blog, "Blog post", "Q", milestone=ms)
            b2 = await make_ticket(blog, "Blog theme", "Q", milestone=ms2)
            store.set_project_lead(blog, pid["R"])
            store.update_ticket(b1["id"], status="done", stage="merged")
            lonely = await make_project("Lonely")   # nobody's tickets, nobody leads it
            await make_ticket(lonely, "Unowned")

        async def push(who, lines):
            async with mk() as cx:
                r = await cx.post("/ingest/claude_code", content=nd(lines),
                                  headers={"Content-Type": "application/x-ndjson",
                                           **(keys[who] if who else root)})
                ck(f"{who or 'root'}'s lines are taken in", r.status_code < 300, r.text)

        async def work(who=None, **params):
            async with mk() as cx:
                r = await cx.get("/api/crew/my-work", params=params,
                                 headers=keys[who] if who else root)
            return r

        def names(rows, key="ticket"):
            return sorted(x[key] for x in rows)

        print("== before any hand-over ==")
        r = await work("P")
        ck("a project that was not handed over is nobody's work yet", r.status_code == 200
           and r.json()["ready"] == [] and r.json()["led_projects"] == [], r.text)

        print("== the hand-over records who and which crew ==")
        os.environ["FACTORY_CREW"] = "a"
        as_(keys["P"])
        out = await mf.hand_over(project="Shop", repos=[{"path": "/w/shop", "name": "acme/shop"}])
        ck("P hands Shop over with crew a", "Recorded" in out, out)
        async with mk() as cx:
            ho = (await cx.get(f"/api/projects/{shop}/crew", headers=keys["P"])).json()["handover"]
        ck("the hand-over names P as the person and crew a",
           ho["handed_by_person"]["id"] == pid["P"] and ho["crew"] == "a"
           and ho["person"]["id"] == pid["P"], ho)
        as_(root)
        os.environ.pop("FACTORY_CREW")
        async with mk() as cx:
            r = await cx.post(f"/api/projects/{blog}/handover", json={"repos": [{"path": "/w/blog"}]})
            ck("root hands Blog over", r.status_code == 200, r.text)
            ho = r.json()["handover"]
            ck("a hand-over with no crew named is filed under `crew`, the root token's person is empty",
               ho["crew"] == "crew" and ho["handed_by_person"] is None, ho)
            r = await cx.post(f"/api/projects/{lonely}/handover", json={"repos": [{"path": "/w/l"}],
                                                                        "crew": "Bad Name"})
            ck("a crew name that cannot be one is refused", r.status_code == 400, r.text)
            r = await cx.post(f"/api/projects/{lonely}/handover", json={"repos": [{"path": "/w/l"}]},
                              headers={"X-Tares-Crew": "viahdr"})
            ck("the crew can come from the X-Tares-Crew header", r.json()["handover"]["crew"] == "viahdr",
               r.text)
            r = await cx.post(f"/api/projects/{lonely}/handover", json={"repos": [{"path": "/w/l"}]},
                              headers=keys["bot"])
            ck("hand_over from a key with no person is refused", r.status_code == 403
               and NO_PERSON in r.text, r.text)

        print("== my work: P and Q each have two tickets in Shop ==")
        r = await work("P", crew="a")
        wp = r.json()
        ck("P's work answers with P and the crew", r.status_code == 200
           and wp["person"] == {"id": pid["P"], "name": "P"} and wp["crew"] == "a", r.text)
        ck("P's ready are P's two tickets, and only those", names(wp["ready"]) == ["T1", "T2"]
           and {x["project"] for x in wp["ready"]} == {"Shop"}, wp["ready"])
        ck("a ready row carries project, ticket, title and repo",
           set(wp["ready"][0]) == {"project", "ticket", "title", "repo"}, wp["ready"][0])
        wq = (await work("Q")).json()
        ck("Q's ready holds the one that waits on nobody; the one waiting on P's T1 is not ready",
           [x["ticket"] for x in wq["ready"] if x["project"] == "Shop"] == ["T4"], wq["ready"])
        ck("Q's work has no crew named: it is `crew`", wq["crew"] == "crew", wq["crew"])
        r = await work("Q", crew=None)
        async with mk() as cx:
            hdr = (await cx.get("/api/crew/my-work", headers={**keys["Q"], "X-Tares-Crew": "qq"})).json()
        ck("the crew comes from the X-Tares-Crew header when no parameter says", hdr["crew"] == "qq", hdr)
        ck("the shape has desk and spec_questions, empty", wp["desk"] == [] and wp["spec_questions"] == []
           and set(wp) == {"person", "crew", "ready", "in_progress", "led_projects", "desk",
                           "spec_questions"}, set(wp))

        print("== a ticket held by a station is in progress, and no longer ready ==")
        store.update_ticket(t2["id"], holder="a-shop-build-m1", stage="doing", status="in_progress")
        wp = (await work("P")).json()
        ck("P's second ticket is in progress with its holder and stage",
           wp["in_progress"] == [{"project": "Shop", "ticket": "T2", "title": "P second",
                                  "holder": "a-shop-build-m1", "stage": "doing"}], wp["in_progress"])
        ck("and P's ready is the first only", names(wp["ready"]) == ["T1"], wp["ready"])
        ck("Q sees none of P's", (await work("Q")).json()["in_progress"] == [])

        print("== P's ticket merges: Q's dependent one becomes ready ==")
        store.update_ticket(t1["id"], stage="merged", status="done")
        wp, wq = (await work("P")).json(), (await work("Q")).json()
        ck("P's ready no longer lists the merged ticket", wp["ready"] == [], wp["ready"])
        ck("Q's dependent ticket is ready now, next to the other",
           [x["ticket"] for x in wq["ready"] if x["project"] == "Shop"] == ["T3", "T4"], wq["ready"])

        print("== a person with no tickets ==")
        wr = (await work("R")).json()
        ck("R has no ready tickets and nothing in progress", wr["ready"] == [] and wr["in_progress"] == []
           and wr["person"]["id"] == pid["R"], wr)
        ck("R leads Blog, whose first milestone has all its tickets delivered",
           wr["led_projects"] == [{"project": "Blog", "milestones_ready_to_check": ["M1"]}],
           wr["led_projects"])
        ck("P leads nothing", wp["led_projects"] == [])
        ck("Q's work in Blog (an open M2 ticket) is ready, the delivered one is not",
           [x["ticket"] for x in wq["ready"] if x["project"] == "Blog"] == ["T2"], wq["ready"])

        print("== a key with no person, and the root token ==")
        r = await work("bot")
        ck("a key with no person is refused", r.status_code == 403 and NO_PERSON in r.text, r.text)
        as_(keys["bot"])
        ck("the tool says so too", NO_PERSON in await mf.my_work(), "")
        as_(root)
        r = await work(None)
        wl = r.json()
        ck("the root token is the person local", r.status_code == 200 and wl["person"]["id"] == "local"
           and wl["person"]["name"] == "You", r.text)
        ck("and local has the tickets nobody owns, in handed-over projects only",
           names([x for x in wl["ready"] if x["project"] == "Lonely"]) == ["T1"]
           and not [x for x in wl["ready"] if x["project"] == "Shop"], wl["ready"])

        print("== /api/crew/my-work is not a station called my-work ==")
        async with mk() as cx:
            r = await cx.get("/api/crew/my-work", headers=keys["P"])
            st = await cx.get("/api/crew/not-a-station", headers=keys["P"])
        ck("it answers the work, not a 404 for a missing station", r.status_code == 200
           and "ready" in r.json() and st.status_code == 404, (r.status_code, r.text[:200]))

        print("== the my_work tool ==")
        as_(keys["P"])
        out = json.loads(await mf.my_work(crew="pa"))
        ck("my_work(crew) returns P's work in that crew", out["person"]["name"] == "P"
           and out["crew"] == "pa" and names(out["ready"]) == [], out)
        os.environ["FACTORY_CREW"] = "envcrew"
        out = json.loads(await mf.my_work())
        ck("an empty crew means the caller's own (FACTORY_CREW)", out["crew"] == "envcrew", out)
        os.environ.pop("FACTORY_CREW")
        out = json.loads(await mf.my_work())
        ck("and with neither, `crew`", out["crew"] == "crew", out)

        print("== crew_projects narrows to the person's projects ==")
        async def projects(who):
            async with mk() as cx:
                r = await cx.get("/api/crew/projects", headers=keys[who] if who else root)
            return sorted(p["name"] for p in r.json()["projects"])

        ck("the root token sees every handed-over project", await projects(None) == ["Blog", "Lonely", "Shop"])
        ck("P sees Shop (holds tickets there), not Blog", await projects("P") == ["Shop"])
        ck("Q sees Shop and Blog", await projects("Q") == ["Blog", "Shop"])
        ck("R sees Blog (leads it)", await projects("R") == ["Blog"])
        as_(keys["P"])
        tool = json.loads(await mf.crew_projects())
        ck("the crew_projects tool is narrowed the same way", [p["name"] for p in tool["projects"]] == ["Shop"], tool)

        print("== the hand_over tool names the caller's own crew's orchestrator ==")
        # Q's crew `a` runs an orchestrator named a-orchestrator; P's crew `a` has none
        await push("Q", orch_lines("q-o1", "a-orchestrator", "a", "m2"))
        os.environ["FACTORY_CREW"] = "a"
        as_(keys["P"])
        out = await mf.hand_over(project="Shop", repos=[{"path": "/w/shop"}])
        ck("another person's running a-orchestrator is not P's: it does not say to message it",
           "SendMessage" not in out and "No orchestrator" in out and "a-orchestrator" in out, out)
        ck("and it tells P how other people's crews work", "next tick" in out, out)
        await push("P", orch_lines("p-o1", "a-orchestrator", "a", "m1"))
        out = await mf.hand_over(project="Shop", repos=[{"path": "/w/shop"}])
        ck("with P's own running, it names a-orchestrator and the message to send",
           "a-orchestrator" in out and "[TF:SHIP]" in out and "SendMessage" in out
           and "next tick" in out, out)
        os.environ["FACTORY_CREW"] = "other"
        out = await mf.hand_over(project="Shop", repos=[{"path": "/w/shop"}])
        ck("a crew of P's that is not running says so by name", "other-orchestrator" in out
           and "SendMessage" not in out, out)
        async with mk() as cx:
            ho = (await cx.get(f"/api/projects/{shop}/crew", headers=keys["P"])).json()["handover"]
        ck("the latest hand-over keeps the crew it was made from", ho["crew"] == "other", ho)

        print("== the default crew, and a cell of one person ==")
        os.environ.pop("FACTORY_CREW")
        await push(None, orch_lines("l-o1", "crew-orchestrator", "crew", ""))
        as_(root)
        out = await mf.hand_over(project="Blog", repos=[{"path": "/w/blog"}])
        ck("the root token's hand_over names crew-orchestrator", "crew-orchestrator" in out
           and "[TF:SHIP]" in out, out)
        as_(keys["bot"])
        out = await mf.hand_over(project="Blog", repos=[{"path": "/w/blog"}])
        ck("the tool is refused for a key with no person", NO_PERSON in out, out)
        as_(root)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
