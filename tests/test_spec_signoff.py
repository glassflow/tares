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
from datetime import datetime, timedelta, timezone

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
            return r.json()

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
        row = r.json()["signoffs"][0]
        ck("and says the repo needs no sign-off, so the page does not read 'No area owner' or "
           "'Waiting'", row["exempt"] == "solo" and row["signed_off"] is False, r.text)
        ck("the repos ready count counts it as ready", sum(
            1 for x in r.json()["signoffs"] if x["exempt"] or x["signed_off"]) == 1, r.text)
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

        r = await call("ashish", "GET", f"/api/projects/{uid}/signoffs")
        rows_ = {x["repo"]: x for x in r.json()["signoffs"]}
        ck("the lead's own repo needs no sign-off", rows_["acme/api"]["exempt"] == "lead"
           and rows_["acme/api"]["is_lead"] is True, r.text)
        ck("another person's repo is waiting: owner kiran, not exempt, not signed",
           rows_["acme/web"]["exempt"] is None and rows_["acme/web"]["owner"]["name"] == "kiran"
           and rows_["acme/web"]["signed_off"] is False, r.text)
        ck("the ready count is 1 of 2", sum(1 for x in r.json()["signoffs"]
                                            if x["exempt"] or x["signed_off"]) == 1, r.text)

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
        r = await call("ashish", "GET", f"/api/projects/{uid}/questions")
        q1 = r.json()["questions"][0]
        ck("the questions route gives the answerer's name, not only the id",
           (q1["answered_by_person"] or {}).get("name") == "kiran" and q1["answered_by"] == pid["kiran"]
           and (q1["to_person"] or {}).get("name") == "kiran", r.text)
        store.con.execute("UPDATE spec_questions SET answered_by = 'p_gone' WHERE project = ? AND id = 'Q1'",
                          [uid])
        q1 = (await call("ashish", "GET", f"/api/projects/{uid}/questions")).json()["questions"][0]
        ck("a person who is gone has no name, and the raw id is all there is", q1["answered_by_person"] is None
           and q1["answered_by"] == "p_gone", str(q1))
        store.con.execute("UPDATE spec_questions SET answered_by = ? WHERE project = ? AND id = 'Q1'",
                          [pid["kiran"], uid])
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
        r = await call("ashish", "GET", f"/api/projects/{blocked}/signoffs")
        rows_ = {x["repo"]: x for x in r.json()["signoffs"]}
        ck("on a cell of several people a repo with no owner is not exempt (reads 'No area owner')",
           rows_["acme/db"]["owner"] is None and rows_["acme/db"]["exempt"] is None
           and rows_["acme/db"]["signed_off"] is False, r.text)
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


        print("== the area changes hands: the new owner signs, the old owner's row does not count ==")
        mv = await make_project("ashish", "signoff-moved")
        await ticket("ashish", mv, "web again", "acme/web")
        r = await call("kiran", "POST", f"/api/projects/{mv}/signoffs", {"repo": "acme/web"})
        ck("kiran signs acme/web", r.status_code == 201, r.text)
        first_at = store.list_signoffs(mv)[0]["at"]
        r = await call("kiran", "POST", f"/api/projects/{mv}/signoffs", {"repo": "acme/web"})
        ck("kiran signing again keeps the first row and time", r.status_code == 201
           and len(store.list_signoffs(mv)) == 1 and store.list_signoffs(mv)[0]["at"] == first_at, r.text)
        r = await call("ashish", "PUT", "/api/areas/acme/web", {"person_id": pid["lee"]})
        ck("acme/web moves to lee", r.status_code < 300, r.text)
        r = await call("ashish", "GET", f"/api/projects/{mv}/signoffs")
        ck("kiran's sign-off no longer counts: the repo reads not signed off",
           r.json()["signoffs"][0]["signed_off"] is False and r.json()["signoffs"][0]["owner"]["name"] == "lee"
           and r.json()["waiting_for"] == ["acme/web: lee (not signed off)"], r.text)
        r = await handover("ashish", mv)
        ck("hand-over is refused until lee signs", r.status_code == 409
           and "acme/web: lee (not signed off)" in r.text, r.text)
        r = await call("lee", "POST", f"/api/projects/{mv}/signoffs", {"repo": "acme/web"})
        ck("lee signs: 201", r.status_code == 201, r.text)
        rows = store.list_signoffs(mv)
        ck("the row is lee's now, one row", len(rows) == 1 and rows[0]["person_id"] == pid["lee"], rows)
        r = await call("ashish", "GET", f"/api/projects/{mv}/signoffs")
        ck("the repo reads signed off", r.json()["signoffs"][0]["signed_off"] is True
           and r.json()["waiting_for"] == [], r.text)
        r = await handover("ashish", mv)
        ck("hand-over goes through after the new owner signs", r.status_code == 200
           and r.json()["handover"]["override"] is None, r.text)
        r = await call("ashish", "PUT", "/api/areas/acme/web", {"person_id": pid["kiran"]})
        ck("acme/web moves back to kiran", r.status_code < 300, r.text)
        r = await call("ashish", "GET", f"/api/projects/{mv}/signoffs")
        ck("lee's row does not count for kiran, who has not signed again",
           r.json()["signoffs"][0]["signed_off"] is False
           and r.json()["waiting_for"] == ["acme/web: kiran (not signed off)"], r.text)
        r = await handover("ashish", mv)
        ck("a further hand-over is refused again", r.status_code == 409, r.text)

        print("== a later hand-over keeps the override record ==")
        kp = await make_project("ashish", "signoff-keep")
        await ticket("ashish", kp, "web keep", "acme/web")
        why2 = "kiran is away and it is a one-line change"
        r = await handover("ashish", kp, override_reason=why2)
        ck("the lead overrides", r.status_code == 200 and r.json()["handover"]["override"]["reason"] == why2,
           r.text)
        at1 = store.get_handover(kp)["override"]["at"]
        r = await call("kiran", "POST", f"/api/projects/{kp}/signoffs", {"repo": "acme/web"})
        ck("kiran signs afterwards", r.status_code == 201, r.text)
        r = await handover("ashish", kp)
        ck("a second hand-over with nothing missing is accepted", r.status_code == 200, r.text)
        ov = (store.get_handover(kp).get("override") or {})
        ck("the reason and its time are still there", ov.get("reason") == why2 and ov.get("at") == at1, r.text)
        r = await call("ashish", "GET", f"/api/projects/{kp}/timeline")
        ev = (r.json() or {}).get("events") or []
        ck("and so is the timeline event", len(ev) == 1 and ev[0]["kind"] == "handover_override"
           and why2 in ev[0]["text"], r.text)

        print("== an override stays with the person who made it (TR-640) ==")
        f6 = await make_project("ashish", "signoff-f6")
        await ticket("ashish", f6, "web f6", "acme/web")
        r = await handover("ashish", f6, override_reason="kiran is away")
        ck("the lead overrides", r.status_code == 200, r.text)
        r = await call("kiran", "POST", f"/api/projects/{f6}/signoffs", {"repo": "acme/web"})
        ck("kiran signs afterwards", r.status_code == 201, r.text)
        r = await handover("lee", f6)
        ck("lee hands over again, with no reason", r.status_code == 200
           and (r.json()["handover"]["person"] or {}).get("id") == pid["lee"], r.text)
        ho = store.get_handover(f6)
        ck("the override is still ashish's, not lee's", (ho["override"]["person"] or {}).get("id")
           == pid["ashish"] and "ashish" in (ho["override"]["by_label"] or ""), str(ho["override"]))
        row = store.con.execute("SELECT override_person_id FROM handovers WHERE project = ?",
                                [f6]).fetchone()
        ck("override_person_id is stored", row[0] == pid["ashish"], row)
        r = await call("ashish", "GET", f"/api/projects/{f6}/timeline")
        ev = (r.json() or {}).get("events") or []
        ck("and the timeline event names ashish", len(ev) == 1 and "ashish" in (ev[0]["by"] or "")
           and "lee" not in (ev[0]["by"] or ""), r.text)
        store.con.execute("UPDATE handovers SET override_person_id = NULL WHERE project = ?", [f6])
        ck("a row from before the column falls back to the handing person",
           (store.get_handover(f6)["override"]["person"] or {}).get("id") == pid["lee"])

        print("== every override is listed, once, across timeline pages (TR-640) ==")
        ov = await make_project("ashish", "signoff-overrides")
        await ticket("ashish", ov, "web ov", "acme/web")
        t0 = datetime.now(timezone.utc) - timedelta(days=10)
        hours = lambda n: t0 + timedelta(hours=n)
        for i, h in enumerate((10, 20, 30)):   # three threads: runs started at +10h, +20h, +30h
            rid = f"ov-run-{i}"
            store.start_agent_run(rid, "watcher", "t", "", "k", "h", project=ov)
            store.con.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [hours(h), rid])
        repos = [{"path": os.path.join(_TMP, "r"), "name": "acme/api"}]
        # +5h older than every thread, +15h and +25h between them, +35h newer than all
        for h, who, why_ in ((5, "ashish", "first, old"), (15, "lee", "second, by lee"),
                             (25, "ashish", "third"), (35, "lee", "fourth, newest")):
            store.set_handover(ov, repos, None, "", hours(h), pid[who], None, why_)
        listed = store.list_handover_overrides(ov)
        ck("the log keeps all four, oldest first", [o["reason"] for o in listed]
           == ["first, old", "second, by lee", "third", "fourth, newest"], str(listed))
        ck("with the person of each", [(o["person"] or {}).get("id") for o in listed]
           == [pid["ashish"], pid["lee"], pid["ashish"], pid["lee"]], str(listed))
        ck("the hand-over itself shows them all, its own override being the latest",
           [o["reason"] for o in store.get_handover(ov)["overrides"]] == [o["reason"] for o in listed]
           and store.get_handover(ov)["override"]["reason"] == "fourth, newest")
        r = await call("ashish", "GET", f"/api/projects/{ov}/timeline")
        ev = r.json().get("events") or []
        ck("one page with room for all threads carries all four events", [e["reason"] for e in ev]
           == ["first, old", "second, by lee", "third", "fourth, newest"]
           and "lee" in (ev[1]["by"] or "") and "ashish" in (ev[0]["by"] or ""), r.text)
        pages, before, seen_ev = [], None, []
        for _ in range(8):
            params = {"limit": 1}
            if before:
                params["before"] = before
            r = await call("ashish", "GET", f"/api/projects/{ov}/timeline", **params)
            body = r.json()
            pages.append(([t["id"] for t in body["threads"]], [e["reason"] for e in body.get("events") or []]))
            seen_ev += [e["reason"] for e in body.get("events") or []]
            before = body["next_before"]
            if not before:
                break
        ck("paged one thread at a time: three threads then an empty last page", len(pages) == 4
           and [len(t) for t, _ in pages] == [1, 1, 1, 0], str(pages))
        ck("each event sits on the page whose window holds it", [e for _, e in pages] ==
           [["fourth, newest"], ["third"], ["second, by lee"], ["first, old"]], str(pages))
        ck("none repeats and none is lost across the pages", sorted(seen_ev) == sorted(
            ["first, old", "second, by lee", "third", "fourth, newest"]), str(seen_ev))

        print("== canceled tickets do not hold the hand-over ==")
        cp = await make_project("ashish", "signoff-canceled")
        await ticket("ashish", cp, "api part", "acme/api")
        gone = await ticket("ashish", cp, "dropped ghost work", "acme/ghost")
        r = await call("ashish", "PUT", f"/api/projects/{cp}/tickets/{gone.get('ref') or gone['id']}",
                       {"status": "canceled"})
        ck("the ticket in acme/ghost is canceled", r.status_code == 200, r.text)
        r = await call("ashish", "GET", f"/api/projects/{cp}/signoffs")
        ck("acme/ghost is not listed and nothing is awaited", [x["repo"] for x in r.json()["signoffs"]]
           == ["acme/api"] and r.json()["waiting_for"] == [], r.text)
        r = await handover("ashish", cp)
        ck("hand-over goes through with no owner for the canceled repo", r.status_code == 200, r.text)

        print("== a repo the project has no tickets in cannot be signed, even by its owner ==")
        nt = await make_project("ashish", "signoff-no-tickets")
        await ticket("ashish", nt, "api only", "acme/api")
        r = await call("kiran", "POST", f"/api/projects/{nt}/signoffs", {"repo": "acme/web"})
        ck("kiran owns acme/web but the project has no tickets there: 409", r.status_code == 409
           and "the project has no tickets in acme/web" in r.text, r.text)
        ck("no row was written", store.list_signoffs(nt) == [])

        print("== the override reason is cut to 500 characters ==")
        lp = await make_project("ashish", "signoff-long-reason")
        await ticket("ashish", lp, "web long", "acme/web")
        r = await handover("ashish", lp, override_reason="x" * 700)
        ck("accepted, stored as 500", r.status_code == 200
           and len(r.json()["handover"]["override"]["reason"]) == 500, r.text)

        print("== a private project's questions stay out of a person's my_work who cannot see it ==")
        store.add_spec_question(priv, pid["kiran"], "acme/web", "private question", pid["ashish"],
                                datetime.now(timezone.utc))
        ck("the question exists, open, for kiran", any(
            q["to_person_id"] == pid["kiran"] for q in store.list_spec_questions(priv, "open")))
        r = await call("kiran", "GET", "/api/crew/my-work")
        ck("kiran's my_work does not list it", all(q["project"] != "signoff-private"
                                                    for q in r.json()["spec_questions"])
           and "private question" not in r.text, r.text)

        print("== deleting a project takes its questions and sign-offs ==")
        dp = await make_project("ashish", "signoff-doomed")
        await ticket("ashish", dp, "web doomed", "acme/web")
        r = await call("ashish", "POST", f"/api/projects/{dp}/questions", {"repo": "acme/web", "text": "q"})
        ck("a question on it", r.status_code == 201, r.text)
        r = await call("kiran", "POST", f"/api/projects/{dp}/signoffs", {"repo": "acme/web"})
        ck("a sign-off on it", r.status_code == 201, r.text)
        store.set_handover(dp, [{"path": "/w/x"}], None, "", datetime.now(timezone.utc), pid["ashish"],
                           None, "doomed override")
        count = lambda t: store.con.execute(f"SELECT COUNT(*) FROM {t} WHERE project = ?", [dp]).fetchone()[0]
        ck("all three tables hold a row", count("spec_questions") == 1 and count("signoffs") == 1
           and count("handover_overrides") == 1)
        r = await call("ashish", "DELETE", f"/api/projects/{dp}")
        ck("the project is deleted", r.status_code < 300, r.text)
        ck("its questions, sign-offs and overrides are gone", count("spec_questions") == 0
           and count("signoffs") == 0 and count("handover_overrides") == 0)

        print("== an override kept before the log existed becomes its first entry ==")
        old = await make_project("ashish", "signoff-legacy")
        store.set_handover(old, [{"path": "/w/x"}], None, "", datetime.now(timezone.utc), pid["lee"],
                           None, "legacy reason")
        store.con.execute("DELETE FROM handover_overrides WHERE project = ?", [old])
        store._factory_upgrade()
        store._factory_upgrade()
        lg = store.list_handover_overrides(old)
        ck("one entry, once, with its person", len(lg) == 1 and lg[0]["reason"] == "legacy reason"
           and (lg[0]["person"] or {}).get("id") == pid["lee"], str(lg))

        print("== my_work keeps the questions of the project asked for (Q numbers restart per project) ==")
        mp = [await make_project("ashish", f"signoff-mw{i}") for i in (1, 2)]
        for u in mp:
            await ticket("ashish", u, "web mw", "acme/web")
            r = await call("ashish", "POST", f"/api/projects/{u}/questions",
                           {"repo": "acme/web", "text": f"Q for {u}"})
            ck("a Q1 for kiran", r.status_code == 201 and r.json()["id"] == "Q1", r.text)
        mine = lambda out, name: [q for q in out["spec_questions"] if q["project"] == name]
        as_(keys["kiran"])
        out = json.loads(await mf.my_work(project="signoff-mw1"))
        ck("my_work(project=p1) returns only p1's questions, by name", out["spec_questions"] and all(
            q["project"] == "signoff-mw1" for q in out["spec_questions"])
           and [q["id"] for q in out["spec_questions"]] == ["Q1"], str(out["spec_questions"]))
        out = json.loads(await mf.my_work(project=mp[1]))
        ck("an id works too, and returns p2's", [(q["project"], q["id"]) for q in out["spec_questions"]]
           == [("signoff-mw2", "Q1")], str(out["spec_questions"]))
        out = json.loads(await mf.my_work())
        ck("with no project both Q1s come back, as before", len(mine(out, "signoff-mw1")) == 1
           and len(mine(out, "signoff-mw2")) == 1, str(out["spec_questions"])[:300])
        out = json.loads(await mf.my_work(project="signoff-nope"))
        ck("an unknown project returns no questions", out["spec_questions"] == [], str(out))
        r = await call("kiran", "GET", "/api/crew/my-work", project="signoff-mw2")
        ck("the route filters the same way", [q["project"] for q in r.json()["spec_questions"]]
           == ["signoff-mw2"], r.text)

        print("== hand_over says 'without every sign-off' only for the hand-over that was overridden ==")
        hv = await make_project("ashish", "signoff-says")
        await ticket("ashish", hv, "web says", "acme/web")
        as_(keys["ashish"])
        repos_ = [{"path": os.path.join(_TMP, "r"), "name": "acme/api"}]
        out = await mf.hand_over(project="signoff-says", repos=repos_, override_reason="kiran is away")
        ck("the overridden hand-over says so, with the reason",
           "handed over without every sign-off: kiran is away" in out, out)
        ck("get_handover marks it overridden", store.get_handover(hv)["overridden"] is True)
        r = await call("kiran", "POST", f"/api/projects/{hv}/signoffs", {"repo": "acme/web"})
        ck("kiran signs afterwards", r.status_code == 201, r.text)
        out = await mf.hand_over(project="signoff-says", repos=repos_)
        ck("a later clean hand-over does not say it", out.startswith("Recorded")
           and "without every sign-off" not in out, out)
        ho = store.get_handover(hv)
        ck("it is not overridden, though the page still lists the earlier override",
           ho["overridden"] is False and ho["override"]["reason"] == "kiran is away"
           and len(ho["overrides"]) == 1, str(ho))
        out = await mf.hand_over(project="signoff-says", repos=repos_, override_reason="again, still away")
        ck("an override on a hand-over that needed none is not stored", "without every sign-off" not in out
           and store.get_handover(hv)["overridden"] is False and len(store.get_handover(hv)["overrides"]) == 1,
           out)

        print("== the hand-over and its override log entry are saved together ==")
        tx = await make_project("ashish", "signoff-txn")
        t1 = datetime.now(timezone.utc) - timedelta(hours=2)
        store.set_handover(tx, [{"path": "/w/one"}], "L3-review", "first", t1, pid["ashish"], "crewA")
        before_ho = store.get_handover(tx)
        store.con.execute("ALTER TABLE handover_overrides RENAME TO handover_overrides_off")
        try:
            store.set_handover(tx, [{"path": "/w/two"}], None, "second", datetime.now(timezone.utc),
                               pid["lee"], "crewB", "pushed through")
            failed = False
        except Exception:   # noqa: BLE001
            failed = True
        finally:
            store.con.execute("ALTER TABLE handover_overrides_off RENAME TO handover_overrides")
        after_ho = store.get_handover(tx)
        ck("a failing log insert makes the hand-over fail", failed)
        ck("and leaves the hand-over row as it was", after_ho == before_ho and after_ho["override"] is None
           and after_ho["by"] == "first" and after_ho["repos"] == [{"path": "/w/one"}], str(after_ho))
        ck("with no log entry", store.list_handover_overrides(tx) == [])
        store.set_handover(tx, [{"path": "/w/two"}], None, "second", datetime.now(timezone.utc), pid["lee"],
                           "crewB", "pushed through")
        ck("the connection is usable after the rollback: the next save lands, with its entry",
           store.get_handover(tx)["by"] == "second"
           and [o["reason"] for o in store.list_handover_overrides(tx)] == ["pushed through"])

        print("== the one-time backfill copes with an override that has no time ==")
        for name, drop_handed in (("signoff-nullat", False), ("signoff-nullboth", True)):
            nu = await make_project("ashish", name)
            hat = datetime.now(timezone.utc) - timedelta(days=1)
            store.set_handover(nu, [{"path": "/w/x"}], None, "", hat, pid["lee"], None, "no time kept")
            store.con.execute("UPDATE handovers SET override_at = NULL WHERE project = ?", [nu])
            if drop_handed:
                store.con.execute("UPDATE handovers SET handed_at = NULL WHERE project = ?", [nu])
            store.con.execute("DELETE FROM handover_overrides WHERE project = ?", [nu])
            try:
                store._factory_upgrade()
                raised = None
            except Exception as e:   # noqa: BLE001
                raised = repr(e)
            lg = store.list_handover_overrides(nu)
            ck(f"{name}: the upgrade does not raise and makes one entry", raised is None and len(lg) == 1
               and lg[0]["at"] is not None and lg[0]["reason"] == "no time kept", f"{raised} {lg}")
            if not drop_handed:
                ck(f"{name}: the entry takes the hand-over's own time", lg[0]["at"] == hat, str(lg))
            else:
                ck(f"{name}: with no hand-over time either it is stamped now",
                   abs((lg[0]["at"] - datetime.now(timezone.utc)).total_seconds()) < 120, str(lg))
            r = await call("ashish", "GET", f"/api/projects/{nu}/timeline")
            ck(f"{name}: the project timeline loads and carries it", r.status_code == 200
               and [e["reason"] for e in r.json().get("events") or []] == ["no time kept"], r.text)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
