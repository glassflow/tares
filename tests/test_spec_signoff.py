"""Questions to an area owner while a spec is written, and their sign-off before the hand-over (TR-636).

A daemon with auth on. First a cell of one person (only `local`): the hand-over is not held up by
areas or sign-offs. Then three people with a key each (ashish leads the project, kiran owns
acme/web, lee owns nothing): ask_area goes to the owner and a repo with no owner is refused; only
the person a question is for answers it; it shows in their my_work until they do; the hand-over is
refused with the list of who has not signed off, and accepted once the area owner has (the lead's
own repo needs none); only an area owner signs off; a repo with tickets and no area owner blocks;
only the project's lead overrides, the reason is kept and shows on the project's timeline; asks in
parallel threads get distinct ids.

Run: .venv/bin/python tests/test_spec_signoff.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-spec-signoff-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-spec-signoff"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf

logging.getLogger("httpx").setLevel(logging.WARNING)   # the MCP import turns request logs on

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


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

        async def call(who, method, path, body=None, **params):
            hdr = root if who == "root" else keys[who]
            async with mk() as cx:
                return await cx.request(method, path, json=body, params=params or None, headers=hdr)

        async def make_project(who, name):
            r = await call(who, "POST", "/api/projects", {"template": "custom", "name": name,
                                                          "objects": [], "kind": "software_factory"})
            assert r.status_code in (200, 201), r.text
            return r.json()["id"]

        async def ticket(who, uid, title, repo):
            r = await call(who, "POST", f"/api/projects/{uid}/tickets", {"title": title, "repo": repo})
            assert r.status_code == 201, r.text

        async def handover(who, uid, **extra):
            return await call(who, "POST", f"/api/projects/{uid}/handover",
                              {"repos": [{"path": os.path.join(_TMP, "r"), "name": "acme/api"}],
                               **extra})

        print("== a cell of one person: no area owner, no sign-off, and the hand-over goes through ==")
        ck("only local is on the cell", store.only_local_person())
        solo = await make_project("root", "solo")
        await ticket("root", solo, "S-a", "acme/web")
        r = await call("root", "GET", f"/api/projects/{solo}/signoffs")
        ck("signoffs still lists the repo, with no owner", r.status_code == 200
           and r.json()["signoffs"][0]["owner"] is None and r.json()["waiting_for"] == [], r.text)
        r = await handover("root", solo)
        ck("hand-over with an unowned repo and no sign-off: accepted", r.status_code == 200
           and r.json()["handover"]["override"] is None, r.text)
        ck("my_work of local has no spec questions", (await call("root", "GET", "/api/crew/my-work")
                                                      ).json()["spec_questions"] == [])

        print("== a cell of three people ==")
        for who in ("ashish", "kiran", "lee"):
            r = await call("root", "POST", "/api/keys", {"name": f"{who}-laptop", "scopes": ["admin"],
                                                         "person": {"email": f"{who}-ss@example.com",
                                                                    "name": who}})
            keys[who] = {"Authorization": f"Bearer {r.json()['secret']}"}
            pid[who] = r.json()["person"]["id"]
        ck("three people, each with a key", len(set(pid.values())) == 3 and not store.only_local_person())
        for area, who in (("acme/api", "ashish"), ("acme/web", "kiran")):
            r = await call("ashish", "PUT", f"/api/areas/{area}", {"person_id": pid[who]})
            ck(f"{area} is {who}'s", r.status_code < 300, r.text)

        uid = await make_project("ashish", "signoff-main")
        ck("ashish leads the project", store.get_project(uid)["lead_person_id"] == pid["ashish"])
        await ticket("ashish", uid, "api work", "acme/api")
        await ticket("ashish", uid, "web work", "acme/web")

        print("== ask_area goes to the owner of the repo ==")
        as_(keys["ashish"])
        out = json.loads(await mf.ask_area("acme/web", "Which page does the checkout live on?",
                                           project="signoff-main"))
        ck("Q1 is addressed to kiran", out["id"] == "Q1" and out["to"] == "kiran"
           and out["state"] == "open" and out["asked_by"] == "ashish", out)
        out = await mf.ask_area("acme/ghost", "Anyone there?", project="signoff-main")
        ck("a repo with no area owner is refused with the way out", out.startswith("error 409")
           and "acme/ghost has no area owner; set one with set_area" in out, out)
        out = await mf.ask_area("not-a-repo", "x", project="signoff-main")
        ck("a name that is not owner/name is refused", out.startswith("error 400"), out)
        ck("only Q1 exists", [q["id"] for q in store.list_spec_questions(uid)] == ["Q1"])

        print("== it shows in the owner's my_work, and only the owner answers ==")
        r = await call("kiran", "GET", "/api/crew/my-work")
        sq = r.json()["spec_questions"]
        ck("kiran's my_work lists Q1", len(sq) == 1 and sq[0]["id"] == "Q1" and sq[0]["repo"] == "acme/web"
           and sq[0]["project"] == "signoff-main" and sq[0]["asked_by"] == "ashish", r.text)
        ck("ashish's my_work has none", (await call("ashish", "GET", "/api/crew/my-work")
                                         ).json()["spec_questions"] == [])
        r = await call("ashish", "POST", f"/api/projects/{uid}/questions/Q1/answer", {"answer": "mine"})
        ck("the asker cannot answer: 403", r.status_code == 403 and "kiran" in r.text, r.text)
        r = await call("lee", "POST", f"/api/projects/{uid}/questions/Q1/answer", {"answer": "mine"})
        ck("another person cannot answer: 403", r.status_code == 403, r.text)
        as_(keys["lee"])
        out = await mf.answer_question("Q1", "mine", project="signoff-main")
        ck("answer_question as lee: refused", out.startswith("error 403"), out)
        ck("still open", store.get_spec_question(uid, "Q1")["state"] == "open")
        r = await call("kiran", "POST", f"/api/projects/{uid}/questions/Q9/answer", {"answer": "x"})
        ck("an unknown id: 404", r.status_code == 404, r.text)
        as_(keys["kiran"])
        out = json.loads(await mf.answer_question("Q1", "On the cart page.", project="signoff-main"))
        ck("kiran answers", out["state"] == "answered" and out["answer"] == "On the cart page."
           and out["id"] == "Q1", out)
        q = store.get_spec_question(uid, "Q1")
        ck("the record keeps who answered and when", q["answered_by"] == pid["kiran"]
           and q["answered_at"] is not None)
        r = await call("kiran", "POST", f"/api/projects/{uid}/questions/Q1/answer", {"answer": "again"})
        ck("answering twice: 409", r.status_code == 409, r.text)
        ck("it leaves kiran's my_work", (await call("kiran", "GET", "/api/crew/my-work")
                                         ).json()["spec_questions"] == [])
        as_(keys["ashish"])
        lq = json.loads(await mf.list_questions(project="signoff-main"))
        ck("list_questions shows the answer", len(lq) == 1 and lq[0]["answer"] == "On the cart page.", lq)
        ck("list_questions state=open is empty", json.loads(
            await mf.list_questions(state="open", project="signoff-main")) == [])

        print("== the hand-over waits for the area owner ==")
        r = await handover("ashish", uid)
        ck("refused (409), naming acme/web and kiran", r.status_code == 409
           and "acme/web: kiran (not signed off)" in r.text, r.text)
        ck("the lead's own repo is not on the list", "acme/api" not in r.text, r.text)
        ck("nothing was recorded", store.get_handover(uid) is None)
        r = await call("ashish", "GET", f"/api/projects/{uid}/signoffs")
        ck("signoffs says what it waits for", r.json()["waiting_for"] == ["acme/web: kiran (not signed off)"],
           r.text)
        r = await call("ashish", "POST", f"/api/projects/{uid}/signoffs", {"repo": "acme/web"})
        ck("the lead cannot sign off kiran's repo: 403", r.status_code == 403, r.text)
        r = await call("lee", "POST", f"/api/projects/{uid}/signoffs", {"repo": "acme/web"})
        ck("nor can lee: 403", r.status_code == 403, r.text)
        r = await call("kiran", "POST", f"/api/projects/{uid}/signoffs", {"repo": "acme/ghost"})
        ck("a repo with no owner cannot be signed: 409", r.status_code == 409, r.text)
        ck("no sign-off row yet", store.list_signoffs(uid) == [])
        r = await call("kiran", "POST", f"/api/projects/{uid}/signoffs", {"repo": "acme/db"})
        ck("a repo the project has no tickets in: refused", r.status_code in (403, 409), r.text)
        as_(keys["kiran"])
        out = json.loads(await mf.sign_off("acme/web", project="signoff-main"))
        row = next(x for x in out["signoffs"] if x["repo"] == "acme/web")
        ck("kiran signs off acme/web", row["signed_off"] is True and row["owner"]["name"] == "kiran", out)
        out = json.loads(await mf.sign_off("acme/web", project="signoff-main"))
        ck("signing again changes nothing", len(store.list_signoffs(uid)) == 1, out)
        as_(keys["ashish"])
        out = await mf.hand_over(project="signoff-main", repos=[{"path": os.path.join(_TMP, "r"),
                                                                  "name": "acme/api"}])
        ck("hand_over now goes through", out.startswith("Recorded"), out)
        ck("with no override on it", store.get_handover(uid)["override"] is None)

        print("== a repo with tickets and no area owner blocks ==")
        blocked = await make_project("ashish", "signoff-unowned")
        await ticket("ashish", blocked, "db work", "acme/db")
        await ticket("ashish", blocked, "web work", "acme/web")
        r = await call("kiran", "POST", f"/api/projects/{blocked}/signoffs", {"repo": "acme/web"})
        ck("kiran signs acme/web there too", r.status_code == 201, r.text)
        r = await handover("ashish", blocked)
        ck("refused: acme/db has tickets but no area owner", r.status_code == 409
           and "acme/db has tickets but no area owner" in r.text and "acme/web" not in
           r.text.split("waits for the area owners:")[1].split("Ask them")[0], r.text)
        ck("not recorded", store.get_handover(blocked) is None)

        print("== only the lead overrides, and the reason is kept and shown ==")
        why = "kiran is out until Monday and the web part is a one-line change"
        r = await handover("kiran", blocked, override_reason=why)
        ck("kiran (not the lead) cannot override: 403", r.status_code == 403
           and "lead" in r.text, r.text)
        r = await handover("lee", blocked, override_reason=why)
        ck("lee cannot either: 403", r.status_code == 403, r.text)
        ck("still not recorded", store.get_handover(blocked) is None)
        r = await handover("ashish", blocked, override_reason="   ")
        ck("a blank reason is no reason: 409", r.status_code == 409, r.text)
        r = await handover("ashish", blocked, override_reason=why)
        ho = (r.json() or {}).get("handover") or {}
        ck("the lead overrides: 200, reason on the hand-over", r.status_code == 200
           and (ho.get("override") or {}).get("reason") == why
           and ((ho["override"].get("person") or {}).get("id") == pid["ashish"]), r.text)
        r = await call("ashish", "GET", f"/api/projects/{blocked}/timeline")
        ev = (r.json() or {}).get("events") or []
        ck("the project timeline shows it", r.status_code == 200 and len(ev) == 1
           and ev[0]["kind"] == "handover_override" and why in ev[0]["text"], r.text)
        r = await call("ashish", "GET", f"/api/projects/{blocked}/crew")
        ck("and so does the project's crew view", (r.json()["handover"].get("override") or {}
                                                   ).get("reason") == why, r.text)
        r = await call("ashish", "GET", f"/api/projects/{uid}/timeline")
        ck("a project handed over properly has no such event", (r.json() or {}).get("events") in (None, []),
           r.text)
        r = await handover("lee", uid, override_reason="not needed")
        ck("an override nobody needed is not stored", r.status_code == 200
           and r.json()["handover"]["override"] is None, r.text)

        print("== parallel asks never share a number ==")
        pj = await make_project("ashish", "signoff-parallel")
        seen, errs = [], []

        def worker():
            for _ in range(25):
                try:
                    seen.append(store.add_spec_question(pj, pid["kiran"], "acme/web", "q", pid["ashish"],
                                                        datetime.now(timezone.utc))["id"])
                except Exception as e:   # noqa: BLE001
                    errs.append(repr(e))

        ts_ = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts_]
        [t.join() for t in ts_]
        ck("no ask failed", not errs, errs[:2])
        ck("50 asks, 50 distinct ids", len(seen) == 50 and len(set(seen)) == 50, sorted(seen)[:6])
        ck("numbered Q1..Q50", set(seen) == {f"Q{i}" for i in range(1, 51)})
        ids = []

        async def ask_http():
            r = await call("ashish", "POST", f"/api/projects/{pj}/questions",
                           {"repo": "acme/web", "text": "again?"})
            ids.append(r.json()["id"])

        await asyncio.gather(*(ask_http() for _ in range(4)))
        ck("four asks over HTTP at once are distinct too", len(set(ids)) == 4, ids)

        print("== a private project: the owner has to be on it to be asked ==")
        priv = await make_project("ashish", "signoff-private")
        r = await call("ashish", "PATCH", f"/api/projects/{priv}", {"scope": "private"})
        r = await call("ashish", "POST", f"/api/projects/{priv}/questions",
                       {"repo": "acme/web", "text": "hi"})
        ck("asking someone who cannot see it: 403", r.status_code == 403, r.text)
        r = await call("kiran", "GET", f"/api/projects/{priv}/questions")
        ck("and kiran cannot read its questions: 404", r.status_code == 404, r.text)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
