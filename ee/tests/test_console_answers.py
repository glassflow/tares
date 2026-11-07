# Tares enterprise licence, see ee/LICENSE
"""A person answers and agrees from the console (TR-740).

A daemon with auth on and three people with a key each (alex leads the project, kim owns
example-org/web, lee owns nothing). The console sends the person's key and no station header; a
crew session always sends one.

Covers: POST /api/desk/{ref}/reply is for the person the item is addressed to (another person, a
crew station and an answered item are refused), records their words as typed with the mark
"console" and no recorded session, keeps the choice, turns a standing answer into a grant like
one typed in a session (a team grant needs an admin, a project grant over a repo someone else's
lead has not agreed to waits), and keeps or overturns every assumption of the item;
POST /api/grants/{id}/decline ends a waiting grant for the person it waits for, keeping who, when,
from where and why; a question answered from the console carries the mark
"console", from a session it carries none; /api/desk/{ref}/allow is for the person the item is
addressed to.

Run: .venv/bin/python ee/tests/test_console_answers.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

_TMP = tempfile.mkdtemp(prefix="tares-console-answers-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-console-answers"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_LICENSE"] = licensed.test_licence()

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares_ee import mcp_questions as mq
from tares import factory

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


STATION = {"X-Tares-Station": "crew-orchestrator", "X-Tares-Role": "orchestrator"}


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
        keys, pid = {}, {}

        async def call(who, method, path, body=None, station=False, **params):
            hdr = dict(root if who == "root" else keys[who])
            if station:
                hdr.update(STATION)
            # its own client: the shared one (for the MCP tools) carries a station header
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://t") as cx:
                return await cx.request(method, path, json=body, params=params or None, headers=hdr)

        for who, scopes in (("alex", ["admin"]), ("kim", ["read", "ingest"]),
                            ("lee", ["read", "ingest"])):
            r = await call("root", "POST", "/api/keys", {
                "name": f"{who}-laptop", "scopes": scopes,
                "person": {"email": f"{who}-ca@example.com", "name": who}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]
        ck("three people, each with a key", len(set(pid.values())) == 3, str(pid))
        r = await call("root", "POST", "/api/keys", {"name": "alex-plain", "scopes": ["read", "ingest"],
                                                     "person_id": pid["alex"]})
        keys["alex_plain"] = {"Authorization": f"Bearer {r.json()['secret']}"}   # no admin scope
        for who in ("kim", "lee"):      # /allow is an admin-scope write, whoever it is for
            r = await call("root", "POST", "/api/keys", {"name": f"{who}-admin", "scopes": ["admin"],
                                                         "person_id": pid[who]})
            keys[who + "_admin"] = {"Authorization": f"Bearer {r.json()['secret']}"}
        for area, who in (("example-org/api", "alex"), ("example-org/web", "kim")):
            r = await call("root", "PUT", f"/api/areas/{area}", {"person_id": pid[who]})
            ck(f"{area} is {who}'s", r.status_code < 300, r.text)

        r = await call("alex", "POST", "/api/projects", {"template": "custom", "name": "shop",
                                                         "objects": [], "kind": "software_factory"})
        uid = r.json()["id"]
        ck("alex leads the project", store.get_project(uid)["lead_person_id"] == pid["alex"])

        async def ticket(title, repo, who):
            r = await call("alex", "POST", f"/api/projects/{uid}/tickets",
                           {"title": title, "repo": repo, "assignee": pid[who]})
            assert r.status_code == 201, r.text
            return factory.label(r.json())

        t_web = await ticket("web work", "example-org/web", "kim")
        t_api = await ticket("api work", "example-org/api", "alex")

        async def add(question, who="alex", **kw):
            as_({**keys[who], **STATION})
            out = await mf.desk_add(question, "yes", "because", project="shop", **kw)
            return json.loads(out) if out.startswith("{") else out

        def reply(who, item, body, station=False):
            return call(who, "POST", f"/api/desk/{item['label']}/reply", body, station=station)

        print("== a reply is for the person the item is addressed to ==")
        d1 = await add("Shop: one page or two?", tickets=[t_web])
        ck("an item about kim's ticket is addressed to kim", d1["to_person"]["id"] == pid["kim"], d1)
        r = await reply("lee", d1, {"words": "Two pages"})
        ck("lee cannot answer kim's item: 403", r.status_code == 403 and "kim" in r.text, r.text)
        r = await reply("alex", d1, {"words": "Two pages"})
        ck("nor can alex, who asked and holds the admin scope: 403", r.status_code == 403, r.text)
        r = await reply("kim", d1, {"words": "Two pages"}, station=True)
        ck("a crew station sending kim's key is refused: 403", r.status_code == 403
           and "console" in r.text, r.text)
        r = await reply("kim", d1, {"words": "   "})
        ck("empty words are a 400", r.status_code == 400, r.text)
        ck("the item is still open after all of that", store.get_desk_item(d1["label"])["state"] == "open")

        print("== kim answers from the console ==")
        r = await reply("kim", d1, {"words": "Two pages, a quick one", "choice": "two pages"})
        out = r.json() if r.status_code == 200 else {}
        ck("it is recorded", r.status_code == 200 and out.get("recorded") is True
           and out.get("granted") is False, r.text)
        ck("not matched in any session, and said so", out.get("matched") is False, r.text)
        dec = out.get("decision", {})
        ck("the decision is kim's, in their words, with the choice", dec.get("words")
           == "Two pages, a quick one" and (dec.get("person") or {}).get("id") == pid["kim"]
           and dec.get("choice") == "two pages", str(dec))
        ck("the decision says console and names no session", dec.get("via") == "console"
           and not dec.get("session"), str(dec))
        ck("the item is answered", store.get_desk_item(d1["label"])["state"] == "answered")
        r = await reply("kim", d1, {"words": "Actually three"})
        ck("answering it again: 409", r.status_code == 409, r.text)

        print("== a standing answer becomes a grant ==")
        d2 = await add("Shop: may builders change the checkout page?", tickets=[t_web])
        r = await reply("kim", d2, {"words": "Builders may change the checkout page in every project",
                                    "standing": True, "scope": "all"})
        ck("a team grant needs an admin: kim gets 403", r.status_code == 403, r.text)
        ck("and the item stays open", store.get_desk_item(d2["label"])["state"] == "open")
        r = await reply("kim", d2, {"words": "Builders may change the checkout page",
                                    "standing": True})
        out = r.json() if r.status_code == 200 else {}
        g = out.get("grant") or {}
        ck("a project grant over kim's own repo, from someone who does not lead the project, "
           "waits for the lead", r.status_code == 200 and g.get("state") == "waiting"
           and out.get("granted") is False and [w["id"] for w in g.get("waiting_for", [])]
           == [pid["alex"]], r.text)
        ck("it is given by kim, in the project", (g.get("given_by") or {}).get("id") == pid["kim"]
           and g.get("project") == uid, str(g))
        ck("the decision is marked console", out.get("decision", {}).get("via") == "console")

        r = await call("lee", "POST", f"/api/grants/{g['id']}/consent")
        ck("lee cannot agree to it: 403", r.status_code == 403, r.text)
        r = await call("alex", "POST", f"/api/grants/{g['id']}/consent")
        g1 = (r.json() if r.status_code == 200 else {}).get("grant", {})
        ck("alex agrees from the console: it is in effect", r.status_code == 200
           and g1.get("in_effect") is True, r.text)
        ck("the agreement is marked console", [c.get("via") for c in g1.get("consents", [])]
           == ["console"], str(g1))
        r = await call("alex", "POST", f"/api/grants/{g['id']}/decline", {})
        ck("declining a grant already in effect: 409", r.status_code == 409, r.text)

        print("== declining a waiting grant ==")
        d3 = await add("Shop: may builders push to the web repo?", tickets=[t_web])
        r = await reply("kim", d3, {"words": "Builders may push to the web repo", "standing": True})
        g2 = (r.json() if r.status_code == 200 else {}).get("grant", {})
        ck("a second waiting grant", g2.get("state") == "waiting", r.text)
        r = await call("lee", "POST", f"/api/grants/{g2['id']}/decline", {"reason": "not mine"})
        ck("lee, who it does not wait for, cannot decline: 403", r.status_code == 403, r.text)
        r = await call("kim", "POST", f"/api/grants/{g2['id']}/decline", {})
        ck("nor can kim, who gave it: 403", r.status_code == 403, r.text)
        r = await call("alex_plain", "POST", f"/api/grants/{g2['id']}/decline",
                       {"reason": "Pushes go through the review first"})
        g3 = (r.json() if r.status_code == 200 else {}).get("grant", {})
        ck("alex declines: it ends and is not in effect", r.status_code == 200
           and g3.get("state") == "revoked" and g3.get("in_effect") is False, r.text)
        c = (g3.get("consents") or [{}])[-1]
        ck("it keeps who declined, from where and why", c.get("id") == pid["alex"]
           and c.get("declined") is True and c.get("via") == "console"
           and c.get("reason") == "Pushes go through the review first" and c.get("at"), str(g3))
        ck("the grant row is revoked in the store", store.get_grant_row(g2["id"])["state"] == "revoked")
        r = await call("alex", "POST", f"/api/grants/{g2['id']}/decline", {})
        ck("declining it again: 409", r.status_code == 409, r.text)
        ck("the store's guarded decline refuses a grant that is no longer waiting, and keeps its row",
           store.decline_waiting_grant(g2["id"], []) is False
           and len(store.get_grant_row(g2["id"])["consents"]) == 1
           and store.decline_waiting_grant(g["id"], []) is False)
        r = await call("alex", "POST", "/api/grants/G999/decline", {})
        ck("an unknown grant: 404", r.status_code == 404, r.text)

        print("== a standing click on an option keeps the option, not its letter ==")
        d3b = await add("Shop: which may builders do?", tickets=[t_web],
                        options=["Builders may merge to web", "Builders may deploy web previews"])
        r = await reply("kim", d3b, {"words": "B", "choice": "Builders may deploy web previews",
                                     "standing": True})
        out = r.json() if r.status_code == 200 else {}
        g4 = out.get("grant") or {}
        ck("the grant says the option, not 'B'", r.status_code == 200
           and g4.get("words") == "Builders may deploy web previews", r.text)
        ck("the decision keeps the choice", (out.get("decision") or {}).get("choice")
           == "Builders may deploy web previews", r.text)

        print("== assumptions: keep or overturn every one ==")
        as_({**keys["alex"], **STATION})
        a1 = json.loads(await mf.assume("Which db?", "sqlite", "small", tickets=[t_web],
                                        affects="kim", project="shop"))["label"]
        a2 = json.loads(await mf.assume("Which cache?", "none", "small", tickets=[t_web],
                                        affects="kim", project="shop"))["label"]
        d4 = await add("Shop: confirm the assumptions", tickets=[t_web], assumptions=[a1, a2])
        r = await reply("kim", d4, {"words": "Keep the first", "kept": [a1]})
        ck("leaving an assumption out is a 400 and records nothing", r.status_code == 400
           and a2 in r.text and store.get_desk_item(d4["label"])["state"] == "open", r.text)
        r = await reply("kim", d4, {"words": "Keep the db, but use redis for the cache",
                                    "kept": [a1], "overturned": {a2: "use redis for the cache"}})
        out = r.json() if r.status_code == 200 else {}
        ck("kept and overturned, each with its state", r.status_code == 200 and sorted(
            (x["label"], x["state"]) for x in out.get("assumptions", []))
            == sorted([(a1, "kept"), (a2, "overturned")]), r.text)
        ck("an overturned one names the tickets to redo", [f["assumption"] for f in out.get(
            "follow_up", [])] == [a2], r.text)
        ck("and the store agrees", store.get_assumption(uid, a1)["state"] == "kept"
           and store.get_assumption(uid, a2)["state"] == "overturned")

        print("== a permission is the addressed person's to allow ==")
        d5 = await add("Shop: run git push?", tickets=[t_web], command="git push",
                  station="shop-build-m1")
        r = await call("lee_admin", "POST", f"/api/desk/{d5['label']}/allow", {"words": "shop-build-m1 may run `git push` in shop", "scope": "project"})
        ck("lee cannot allow kim's command: 403", r.status_code == 403 and "kim" in r.text, r.text)
        r = await call("kim_admin", "POST", f"/api/desk/{d5['label']}/allow", {"words": "shop-build-m1 may run `git push` in shop", "scope": "project"}, station=True)
        ck("a crew station cannot allow it either: 403", r.status_code == 403, r.text)
        ck("the item is still open", store.get_desk_item(d5["label"])["state"] == "open")

        print("== a question answered, from the console and from a session ==")
        as_({**keys["alex"], **STATION})
        q1 = json.loads(await mq.ask_area("example-org/web", "Which page is checkout on?",
                                          project="shop"))
        q2 = json.loads(await mq.ask_area("example-org/web", "Which page is the cart on?",
                                          project="shop"))
        r = await call("kim", "POST", f"/api/projects/{uid}/questions/{q1['id']}/answer",
                       {"answer": "The cart page."})
        ck("kim answers Q1 from the console", r.status_code == 200 and r.json().get("via") == "console",
           r.text)
        r = await call("kim", "POST", f"/api/projects/{uid}/questions/{q2['id']}/answer",
                       {"answer": "The home page."}, station=True)
        ck("kim answers Q2 from a session: no mark", r.status_code == 200 and not r.json().get("via"),
           r.text)
        r = await call("alex", "GET", f"/api/projects/{uid}/questions")
        marks = {x["id"]: x.get("via") for x in r.json()["questions"]}
        ck("the questions list shows both marks", marks == {q1["id"]: "console", q2["id"]: None},
           str(marks))

        print("== finishing the project on a console answer ==")
        for t in (t_web, t_api):
            for stage in ("doing", "merged"):
                await call("alex", "POST", f"/api/projects/{uid}/tickets/{t}/stage", {"stage": stage})
        store.set_handover(uid, [{"path": "/w/shop"}], None, None, datetime.now(timezone.utc))
        fin = lambda ref: call("alex", "POST", f"/api/projects/{uid}/finish", {"decision": ref},
                               station=True)
        d6 = await add("Shop: run git status?", tickets=[t_web], command="git status",
                       station="shop-build-m1")
        r = await call("kim_admin", "POST", f"/api/desk/{d6['label']}/allow", {
            "words": "shop-build-m1 may run `git status` in shop", "scope": "project"})
        ck("kim allows a command in the console", r.status_code == 200, r.text)
        r = await fin(d6["label"])
        ck("a permission allowed in the console is not the person saying the project is done: 409",
           r.status_code == 409 and "no answer from the person" in r.text, r.text)
        d7 = await add("Shop: is the project done?")
        r = await fin(d7["label"])
        ck("an open question: 409", r.status_code == 409, r.text)
        r = await reply("alex", d7, {"words": "Yes, ship it"})
        ck("alex answers it in the console", r.status_code == 200 and r.json().get("matched") is False,
           r.text)
        r = await fin(d7["label"])
        ck("the project finishes on that answer", r.status_code == 200
           and r.json()["finished"]["words"] == "Yes, ship it"
           and r.json()["finished"]["decision"] == d7["label"], r.text)
        ck("the project is done", store.get_project(uid)["status"] == "done")

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
