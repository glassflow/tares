# Tares enterprise licence, see ee/LICENSE
"""Questions to an area owner while a spec is written (TR-636), and a hand-over that waits for
nobody's sign-off.

A daemon with auth on. First a cell of one person (only `local`). Then three people with a key each
(ashish leads the project, kiran owns acme/web, lee owns nothing): ask_area goes to the owner and a
repo with no owner is refused; only the person a question is for answers it; it shows in their
my_work until they do; the hand-over goes through whoever owns the repos, and the sign-off routes
and tool are gone; asks in parallel threads get distinct ids. The overrides kept from before stay
readable on the hand-over and the timeline.

Run: .venv/bin/python ee/tests/test_spec_questions.py
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
import licensed   # scrubs the machine's TARES_* settings and puts the repo and tests/ on sys.path
licensed.install_test_root()

ROOT = licensed.REPO
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-spec-signoff-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_LICENSE"] = licensed.test_licence()
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-spec-signoff"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares_ee import mcp_questions as mq

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

        print("== a cell of one person: no area owner, and the hand-over goes through ==")
        ck("only local is on the cell", store.only_local_person())
        solo = await make_project("root", "solo")
        await ticket("root", solo, "S-a", "acme/web")
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
        out = json.loads(await mq.ask_area("acme/web", "Which page does the checkout live on?",
                                           project="signoff-main"))
        ck("Q1 is addressed to kiran", out["id"] == "Q1" and out["to"] == "kiran"
           and out["state"] == "open" and out["asked_by"] == "ashish", out)
        out = await mq.ask_area("acme/ghost", "Anyone there?", project="signoff-main")
        ck("a repo with no area owner is refused with the way out", out.startswith("error 409")
           and "acme/ghost has no owner on this cell" in out, out)
        out = await mq.ask_area("not-a-repo", "x", project="signoff-main")
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
        out = await mq.answer_question("Q1", "mine", project="signoff-main")
        ck("answer_question as lee: refused", out.startswith("error 403"), out)
        ck("still open", store.get_spec_question(uid, "Q1")["state"] == "open")
        r = await call("kiran", "POST", f"/api/projects/{uid}/questions/Q9/answer", {"answer": "x"})
        ck("an unknown id: 404", r.status_code == 404, r.text)
        as_(keys["kiran"])
        out = json.loads(await mq.answer_question("Q1", "On the cart page.", project="signoff-main"))
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
        store.sc.execute("UPDATE spec_questions SET answered_by = 'p_gone' WHERE project = ? AND id = 'Q1'",
                          [uid])
        q1 = (await call("ashish", "GET", f"/api/projects/{uid}/questions")).json()["questions"][0]
        ck("a person who is gone has no name, and the raw id is all there is", q1["answered_by_person"] is None
           and q1["answered_by"] == "p_gone", str(q1))
        store.sc.execute("UPDATE spec_questions SET answered_by = ? WHERE project = ? AND id = 'Q1'",
                          [pid["kiran"], uid])
        ck("it leaves kiran's my_work", (await call("kiran", "GET", "/api/crew/my-work")
                                         ).json()["spec_questions"] == [])
        as_(keys["ashish"])
        lq = json.loads(await mq.list_questions(project="signoff-main"))
        ck("list_questions shows the answer", len(lq) == 1 and lq[0]["answer"] == "On the cart page.", lq)
        ck("list_questions state=open is empty", json.loads(
            await mq.list_questions(state="open", project="signoff-main")) == [])

        print("== the hand-over waits for nobody's sign-off ==")
        r = await call("ashish", "GET", f"/api/projects/{uid}/signoffs")
        ck("the sign-off route is gone", r.status_code in (404, 405), r.text)
        r = await call("kiran", "POST", f"/api/projects/{uid}/signoffs", {"repo": "acme/web"})
        ck("and nobody can sign off any more", r.status_code in (404, 405), r.text)
        ck("the crew has no sign_off tool", not hasattr(mf, "sign_off"))
        await call("ashish", "POST", f"/api/projects/{uid}/questions", {"repo": "acme/web", "text": "Still there?"})
        as_(keys["ashish"])
        out = await mf.hand_over(project="signoff-main", repos=[{"path": os.path.join(_TMP, "r"),
                                                                  "name": "acme/api"}])
        ck("kiran's repo has nobody's sign-off, a question is even open: hand_over goes through",
           out.startswith("Recorded"), out)
        ck("with no override on it", store.get_handover(uid)["override"] is None)
        blocked = await make_project("ashish", "signoff-unowned")
        await ticket("ashish", blocked, "db work", "acme/db")
        r = await handover("ashish", blocked)
        ck("a repo with tickets and no area owner holds nothing up either", r.status_code == 200, r.text)
        nl = await make_project("lee", "signoff-by-lee")
        await ticket("lee", nl, "web by lee", "acme/web")
        r = await handover("lee", nl, override_reason="kiran is away")
        ck("an override_reason is no longer a thing: accepted and not kept", r.status_code == 200
           and r.json()["handover"]["override"] is None and store.list_handover_overrides(nl) == [], r.text)
        r = await call("ashish", "GET", f"/api/projects/{uid}/timeline")
        ck("a hand-over leaves no override event on the timeline", (r.json() or {}).get("events") in (None, []),
           r.text)

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


        print("== every override is listed, once, across timeline pages (TR-640) ==")
        ov = await make_project("ashish", "signoff-overrides")
        await ticket("ashish", ov, "web ov", "acme/web")
        t0 = datetime.now(timezone.utc) - timedelta(days=10)
        hours = lambda n: t0 + timedelta(hours=n)
        for i, h in enumerate((10, 20, 30)):   # three threads: runs started at +10h, +20h, +30h
            rid = f"ov-run-{i}"
            store.start_agent_run(rid, "watcher", "t", "", "k", "h", project=ov)
            store.sc.execute("UPDATE agent_runs SET started_at = ? WHERE id = ?", [hours(h), rid])
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

        print("== a private project's questions stay out of a person's my_work who cannot see it ==")
        store.add_spec_question(priv, pid["kiran"], "acme/web", "private question", pid["ashish"],
                                datetime.now(timezone.utc))
        ck("the question exists, open, for kiran", any(
            q["to_person_id"] == pid["kiran"] for q in store.list_spec_questions(priv, "open")))
        r = await call("kiran", "GET", "/api/crew/my-work")
        ck("kiran's my_work does not list it", all(q["project"] != "signoff-private"
                                                    for q in r.json()["spec_questions"])
           and "private question" not in r.text, r.text)

        print("== deleting a project takes its questions, old sign-offs and overrides ==")
        dp = await make_project("ashish", "signoff-doomed")
        await ticket("ashish", dp, "web doomed", "acme/web")
        r = await call("ashish", "POST", f"/api/projects/{dp}/questions", {"repo": "acme/web", "text": "q"})
        ck("a question on it", r.status_code == 201, r.text)
        store.sc.execute("INSERT INTO signoffs (project, repo, person_id, \"at\") VALUES (?, 'acme/web', ?, now())",
                          [dp, pid["kiran"]])   # a sign-off kept from before
        store.set_handover(dp, [{"path": "/w/x"}], None, "", datetime.now(timezone.utc), pid["ashish"],
                           None, "doomed override")
        count = lambda t: store.sc.execute(f"SELECT COUNT(*) FROM {t} WHERE project = ?", [dp]).fetchone()[0]
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
        store.sc.execute("DELETE FROM handover_overrides WHERE project = ?", [old])
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
        out = await mf.my_work(project="signoff-nope")
        ck("an unknown project is a clear error naming it, not an empty list",
           out.startswith("error 404:") and "no project 'signoff-nope' you can see" in out
           and "project id or its exact name" in out, out)
        r = await call("kiran", "GET", "/api/crew/my-work", project="signoff-nope")
        ck("the route answers 404 with that message", r.status_code == 404
           and "no project 'signoff-nope' you can see" in r.json()["detail"], r.text)
        r = await call("kiran", "GET", "/api/crew/my-work", project=mp[1])
        ck("an id still works on the route, and a name too", r.status_code == 200
           and [q["id"] for q in r.json()["spec_questions"]] == ["Q1"]
           and (await call("kiran", "GET", "/api/crew/my-work", project="signoff-mw1")).status_code == 200,
           r.text)
        r = await call("kiran", "GET", "/api/crew/my-work", project="signoff-private")
        r2 = await call("kiran", "GET", "/api/crew/my-work", project="signoff-nope-either")
        ck("a project hidden from the caller is the same 404, naming only what was typed",
           r.status_code == 404 and r.json()["detail"].replace("signoff-private", "X")
           == r2.json()["detail"].replace("signoff-nope-either", "X") and "private question" not in r.text,
           r.text)
        r = await call("kiran", "GET", "/api/crew/my-work", project=" ")
        ck("a blank project keeps today's behaviour", r.status_code == 200, r.text)
        r = await call("kiran", "GET", "/api/crew/my-work", project="signoff-mw2")
        ck("the route filters the same way", [q["project"] for q in r.json()["spec_questions"]]
           == ["signoff-mw2"], r.text)

        print("== the hand-over and its override log entry are saved together ==")
        tx = await make_project("ashish", "signoff-txn")
        t1 = datetime.now(timezone.utc) - timedelta(hours=2)
        store.set_handover(tx, [{"path": "/w/one"}], "L3-review", "first", t1, pid["ashish"], "crewA")
        before_ho = store.get_handover(tx)
        store.sc.execute("ALTER TABLE handover_overrides RENAME TO handover_overrides_off")
        try:
            store.set_handover(tx, [{"path": "/w/two"}], None, "second", datetime.now(timezone.utc),
                               pid["lee"], "crewB", "pushed through")
            failed = False
        except Exception:   # noqa: BLE001
            failed = True
        finally:
            store.sc.execute("ALTER TABLE handover_overrides_off RENAME TO handover_overrides")
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
            store.sc.execute("UPDATE handovers SET override_at = NULL WHERE project = ?", [nu])
            if drop_handed:
                store.sc.execute("UPDATE handovers SET handed_at = NULL WHERE project = ?", [nu])
            store.sc.execute("DELETE FROM handover_overrides WHERE project = ?", [nu])
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

        print("== a legacy override with no time reads as overridden after the upgrade ==")
        lo = await make_project("ashish", "signoff-legacy-notime")
        hat = datetime.now(timezone.utc) - timedelta(days=2)
        store.set_handover(lo, [{"path": "/w/x"}], None, "", hat, pid["lee"], None, "old and untimed")
        store.sc.execute("UPDATE handovers SET override_at = NULL WHERE project = ?", [lo])
        store.sc.execute("DELETE FROM handover_overrides WHERE project = ?", [lo])
        ck("before the upgrade it reads as not overridden", store.get_handover(lo)["overridden"] is False)
        store._factory_upgrade()
        ho = store.get_handover(lo)
        ck("after it, overridden, with the hand-over's time", ho["overridden"] is True
           and ho["override"]["at"] == hat, str(ho["override"]))
        first = store.list_handover_overrides(lo)
        store._factory_upgrade()
        ck("a second run changes nothing and adds no second entry", store.get_handover(lo) == ho
           and store.list_handover_overrides(lo) == first and len(first) == 1, str(first))

        print("== two overrides at the same instant come back in one fixed order (not insertion order) ==")
        tie = await make_project("ashish", "signoff-override-tie")
        same = datetime.now(timezone.utc) - timedelta(days=1)
        # inserted "zed" first, then "abe": the order the key list gives is by handed_by, so abe, zed
        store.set_handover(tie, repos, None, "zed", same, pid["ashish"], None, "reason z")
        store.set_handover(tie, repos, None, "abe", same, pid["ashish"], None, "reason a")
        ck("a tie on the time is broken by who handed over, not by insertion order",
           [o["reason"] for o in store.list_handover_overrides(tie)] == ["reason a", "reason z"],
           str(store.list_handover_overrides(tie)))

        print("== an area owner answers with an ordinary person key (no admin scope) ==")
        for who in ("ashish", "kiran", "lee"):
            r = await call("root", "POST", "/api/keys", {"name": f"{who}-plain", "scopes": ["read", "ingest"],
                                                         "person_id": pid[who]})
            keys[who + "_plain"] = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await call("root", "POST", "/api/keys", {"name": "kiran-readonly", "scopes": ["read"],
                                                     "person_id": pid["kiran"]})
        keys["kiran_ro"] = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await call("root", "POST", "/api/keys", {"name": "nobody-plain", "scopes": ["read", "ingest"]})
        keys["nobody"] = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await call("ashish", "PUT", "/api/areas/acme/plain", {"person_id": pid["kiran"]})
        ck("set up: acme/plain is kiran's", r.status_code < 300, r.text)
        pl = await make_project("ashish", "signoff-plain-keys")
        await ticket("ashish", pl, "plain work", "acme/plain")
        await call("ashish", "POST", f"/api/projects/{pl}/questions", {"repo": "acme/plain", "text": "Which port?"})
        r = await call("lee_plain", "POST", f"/api/projects/{pl}/questions/Q1/answer", {"answer": "x"})
        ck("lee's plain key cannot answer kiran's question: 403", r.status_code == 403, r.text)
        r = await call("nobody", "POST", f"/api/projects/{pl}/questions/Q1/answer", {"answer": "x"})
        ck("a plain key with no person cannot answer: 403", r.status_code == 403, r.text)
        r = await call("kiran_ro", "POST", f"/api/projects/{pl}/questions/Q1/answer", {"answer": "x"})
        ck("kiran's read-only key cannot answer: 403", r.status_code == 403, r.text)
        r = await call("root", "POST", "/api/keys", {"name": "pl-project-key", "project": pl})
        keys["projkey"] = {"Authorization": f"Bearer {r.json()['secret']}"}
        r = await call("projkey", "POST", f"/api/projects/{pl}/questions/Q1/answer", {"answer": "x"})
        ck("a project key cannot answer: 403", r.status_code == 403, r.text)
        ck("still open after those", store.get_spec_question(pl, "Q1")["state"] == "open")
        r = await call("kiran_plain", "POST", f"/api/projects/{pl}/questions/Q1/answer", {"answer": "8080"})
        ck("kiran answers with his plain key (REST)", r.status_code == 200 and r.json()["state"] == "answered"
           and r.json()["answer"] == "8080", r.text)
        await call("ashish", "POST", f"/api/projects/{pl}/questions", {"repo": "acme/plain", "text": "Which host?"})
        as_(keys["kiran_plain"])
        out = await mq.answer_question("Q2", "localhost", project="signoff-plain-keys")
        ck("and answers the second through the MCP tool", not out.startswith("error")
           and json.loads(out)["state"] == "answered", out)
        print("== only answering takes an ordinary key; the rest of a project stays admin ==")
        for label, method, path, body in (
            ("asking an area owner", "POST", f"/api/projects/{pl}/questions", {"repo": "acme/plain", "text": "x"}),
            ("handing over", "POST", f"/api/projects/{pl}/handover", {}),
            ("changing the project", "PATCH", f"/api/projects/{pl}", {"scope": "private"}),
        ):
            r = await call("ashish_plain", method, path, body)
            ck(f"a plain key bound to the lead cannot do {label}: 403 on the admin scope",
               r.status_code == 403 and "lacks the 'admin' scope" in r.text, f"{r.status_code} {r.text}")
        pp = await make_project("ashish", "signoff-plain-private")
        await ticket("ashish", pp, "private work", "acme/plain")
        r = await call("ashish", "POST", f"/api/projects/{pp}/questions", {"repo": "acme/plain", "text": "Which port?"})
        ck("set up: a question on the private project", r.status_code == 201, f"{r.status_code} {r.text}")
        r = await call("ashish", "PATCH", f"/api/projects/{pp}", {"scope": "private"})
        ck("set up: a private project", r.status_code < 300, r.text)
        r = await call("lee_plain", "POST", f"/api/projects/{pp}/questions/Q1/answer", {"answer": "x"})
        ck("a plain key of a non-member gets 404 on answering", r.status_code == 404, f"{r.status_code} {r.text}")

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


if __name__ == "__main__":
    asyncio.run(main())
