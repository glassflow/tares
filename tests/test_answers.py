"""The record of the answers people give (TR-1451): every route that takes an answer to something
that waited for them writes one row, and GET /api/answers reads the day's back for the feed.

A desk question answered in the console, and the same from the feed (the X-Tares-Via header), a
permission allowed, a call kept and one overturned, a project put off; each row carries the home
item's id, its ask, yes or no where it had two ways, the words given, who and where. A desk answer
recorded by a station is `session`. A header naming anything else is the console. `since` narrows,
a bad one is refused, `mine` keeps the caller's own. A recorded answer drops the kept
home, so Home read just after no longer shows it waiting.

Run: .venv/bin/python tests/test_answers.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-answers-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-answers"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

from tares import answers
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


ORCH = {"X-Tares-Station": "crew-orchestrator", "X-Tares-Role": "orchestrator"}


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store

    def cx(**headers):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": f"Bearer {TOKEN}", **headers})

    async def post(path, body, **headers):
        async with cx(**headers) as c:
            r = await c.post(path, json=body)
        assert r.status_code < 300, (path, r.status_code, r.text)
        return r.json()

    async def listed(**params):
        async with cx() as c:
            r = await c.get("/api/answers", params=params)
        return r

    async def desk(question, project, **more):
        return await post("/api/desk", {"question": question, "recommendation": "the first",
                                        "why": "it is simpler", "project": project, **more}, **ORCH)

    async with app.router.lifespan_context(app):
        print("== the surface a request names ==")
        req = lambda v: type("R", (), {"headers": {answers.HEADER: v} if v is not None else {}})()
        ck("no header is the console", answers.surface(req(None)) == "console")
        ck("the feed says so", answers.surface(req("feed")) == "feed" and answers.surface(req(" Feed ")) == "feed")
        ck("anything else is the console, `session` too", answers.surface(req("session")) == "console"
           and answers.surface(req("prod")) == "console")
        ck("no request is the console", answers.surface(None) == "console")

        store.create_project("uc_shop", "custom", "Shop", {}, goal="Sell things")
        store.set_project_kind("uc_shop", "software_factory")
        r = await listed()
        ck("nothing answered yet", r.status_code == 200 and r.json() == {"answers": []}, r.text)

        print("== a desk question, in the console and from the feed ==")
        d1 = await desk("Which store should sessions use?", "uc_shop", options=["Redis", "Postgres"])

        async def waiting_ids():
            async with cx() as c:
                return [i["id"] for i in (await c.get("/api/home")).json()["waiting_for_you"]]
        ck("Home shows the question (and keeps that read)", f"question:{d1['label']}" in await waiting_ids())
        await post(f"/api/desk/{d1['label']}/reply", {"words": "Postgres", "choice": "Postgres"})
        ck("the next Home read, within the seconds it keeps one, no longer shows it",
           f"question:{d1['label']}" not in await waiting_ids())
        d2 = await desk("Keep the old login page?", "uc_shop")
        await post(f"/api/desk/{d2['label']}/reply", {"words": "Keep it, redirect"}, **{"X-Tares-Via": "feed"})
        rows = (await listed()).json()["answers"]
        by = {a["id"]: a for a in rows}
        a1 = by.get(f"question:{d1['label']}") or {}
        ck("the console's answer is recorded under the home item's id", a1.get("via") == "console"
           and a1.get("said") == "Postgres" and a1.get("kind") == "question"
           and a1.get("project") == "uc_shop" and a1.get("choice") is None, a1)
        ck("its ask is the one Home showed", a1.get("ask") == "Which store should sessions use?", a1)
        a2 = by.get(f"question:{d2['label']}") or {}
        ck("the feed's answer says it came from the feed", a2.get("via") == "feed"
           and a2.get("said") == "Keep it, redirect", a2)
        ck("newest first", [a["id"] for a in rows][:2] == [f"question:{d2['label']}", f"question:{d1['label']}"],
           [a["id"] for a in rows])
        ck("the root key answers as the local person, so the answer is its own", a1.get("by") == "You"
           and a1.get("mine") is True, a1)

        print("== a permission allowed ==")
        d3 = await desk("builder-1 was refused a command. Allow it?", "uc_shop", station="builder-1",
                        command="git push --force-with-lease")
        await post(f"/api/desk/{d3['label']}/allow", {"words": "builder-1 may force push in Shop",
                                                      "scope": "project"}, **{"X-Tares-Via": "feed"})
        a3 = {a["id"]: a for a in (await listed()).json()["answers"]}.get(f"permission:{d3['label']}") or {}
        ck("allowed is yes, for this project, from the feed", a3.get("choice") == "yes"
           and a3.get("said") == "Allowed for this project" and a3.get("via") == "feed"
           and a3.get("kind") == "permission", a3)
        ck("its ask names the command", "git push --force-with-lease" in (a3.get("ask") or ""), a3)

        print("== calls kept and overturned ==")
        k = await post("/api/projects/uc_shop/assumptions", {"question": "Expired links answer?",
                                                             "choice": "410", "why": "gone"}, **ORCH)
        o = await post("/api/projects/uc_shop/assumptions", {"question": "Expiry checked when?",
                                                             "choice": "on read", "why": "no sweeper"}, **ORCH)
        await post(f"/api/projects/uc_shop/assumptions/{k['label']}/state", {"state": "kept"})
        await post(f"/api/projects/uc_shop/assumptions/{o['label']}/state",
                   {"state": "overturned", "words": "Sweep nightly"}, **ORCH)
        calls = [a for a in (await listed()).json()["answers"] if a["id"] == "calls_to_confirm:uc_shop"]
        ck("one row per call", len(calls) == 2, calls)
        kept = next((a for a in calls if a["choice"] == "yes"), {})
        over = next((a for a in calls if a["choice"] == "no"), {})
        ck("kept is yes with the crew's choice", kept.get("said") == "Kept: 410"
           and kept.get("via") == "console" and kept.get("ask") == "Keep the call: Expired links answer?", kept)
        ck("overturned is no with the person's words, recorded by the station as the session",
           over.get("said") == "Sweep nightly" and over.get("via") == "session", over)
        await post(f"/api/projects/uc_shop/assumptions/{k['label']}/state", {"state": "open"})
        ck("opened again records nothing", len([a for a in (await listed()).json()["answers"]
                                                if a["id"] == "calls_to_confirm:uc_shop"]) == 2)

        print("== a project put off ==")
        store.create_project("uc_billing", "custom", "Billing", {}, goal="Bill")
        await post("/api/projects/uc_billing/later", {"later": True})
        await post("/api/projects/uc_billing/later", {"later": True})
        later = [a for a in (await listed()).json()["answers"] if a["id"] == "ready_to_start:uc_billing"]
        ck("later is no, once, with the hand-over ask", len(later) == 1 and later[0]["choice"] == "no"
           and later[0]["said"] == "Later" and later[0]["ask"] == "Hand Billing to the crew", later)
        await post("/api/projects/uc_billing/later", {"later": False})
        ck("starting it again is not an answer", len([a for a in (await listed()).json()["answers"]
                                                      if a["id"] == "ready_to_start:uc_billing"]) == 1)

        print("== since and mine ==")
        total = len((await listed()).json()["answers"])
        ck("everything was answered today", total == 6, total)
        future = (now_utc() + timedelta(minutes=5)).isoformat()
        ck("since narrows", (await listed(since=future)).json()["answers"] == [])
        ck("since without a zone is UTC", len((await listed(since="2000-01-01T00:00:00")).json()["answers"]) == total)
        r = await listed(since="yesterday")
        ck("since that is not a time is refused", r.status_code == 400 and "since is a time" in r.text, r.text)
        store.add_answer("question:D98", "question", "uc_shop", "Theirs?", None, "no", "console", "pe_other", now_utc())
        mine = (await listed(mine=1)).json()["answers"]
        ck("mine keeps the caller's own", len(mine) == total and all(a["mine"] for a in mine), mine)
        ck("without it, another person's answer is listed too, not as mine",
           any(a["id"] == "question:D98" and not a["mine"] for a in (await listed()).json()["answers"]))
        old = store.add_answer("question:D99", "question", "uc_shop", "Old?", None, "yes", "console", None,
                               now_utc() - timedelta(days=2))
        ck("a row from two days ago is not today's", all(a["at"] != old["at"] for a in (await listed()).json()["answers"]))
        try:
            store.add_answer("x", "question", None, "a", "maybe", "b", "console", None, now_utc())
            ck("a choice other than yes or no is refused", False)
        except ValueError:
            ck("a choice other than yes or no is refused", True)
        ck("a record that fails does not fail the answer", answers.record(
            store, None, item="x", kind="question", project=None, ask="a", said="b", via="nowhere") is None)

        print("== words typed for a question with options stay the person's own ==")
        said = {}
        for words, own in (("Red", True), ("red", False), ("postgres", True)):
            d = await desk(f"Which cache, {words} {own}?", "uc_shop", options=["Redis", "Postgres"])
            await post(f"/api/desk/{d['label']}/reply", {"words": words, "choice": words, **({"own": True} if own else {})})
            said[(words, own)] = {a["id"]: a for a in (await listed()).json()["answers"]}[f"question:{d['label']}"]["said"]
        ck("typed in the text box, the start of an option stays as typed", said[("Red", True)] == "Red", said)
        ck("from a session, the start of an option is the option (TR-1018)", said[("red", False)] == "Redis", said)
        ck("typed, an option's own words are that option", said[("postgres", True)] == "Postgres", said)

        print("== calls kept from Home are recorded under the item that offered them ==")
        c1 = await post("/api/projects/uc_shop/assumptions", {"question": "Retry how often?", "choice": "3", "why": "w"}, **ORCH)
        c2 = await post("/api/projects/uc_shop/assumptions", {"question": "Log where?", "choice": "stdout", "why": "w"}, **ORCH)
        await post(f"/api/projects/uc_shop/assumptions/{c1['label']}/state",
                   {"state": "kept", "item": "calls_to_confirm:uc_shop:pe_alice"})
        await post(f"/api/projects/uc_shop/assumptions/{c2['label']}/state",
                   {"state": "kept", "item": "calls_to_confirm:uc_billing:pe_alice"})
        asks = {a["ask"]: a["id"] for a in (await listed()).json()["answers"]}
        ck("the owners' item id Home showed", asks.get("Keep the call: Retry how often?") == "calls_to_confirm:uc_shop:pe_alice", asks)
        ck("an item of another project is not taken", asks.get("Keep the call: Log where?") == "calls_to_confirm:uc_shop", asks)

        print("== mine survives a busy day of other people's answers ==")
        own_n = len((await listed(mine=1)).json()["answers"])
        for n in range(510):
            store.add_answer(f"question:X{n}", "question", "uc_shop", "Busy?", None, "yes", "console", "pe_other", now_utc())
        busy = (await listed(mine=1)).json()["answers"]
        ck("the caller's own are all there past 500 newer ones", own_n > 0 and len(busy) == own_n
           and all(a["mine"] for a in busy), (own_n, len(busy)))

        print("== a hidden project keeps only the caller's own grants ==")
        at = now_utc()
        store.add_answer("milestone_to_check:uc_vault:m1", "milestone_to_check", "uc_vault", "Check M1 of Vault",
                         "yes", "Accepted", "console", "pe_me", at)
        store.add_answer("grant:G9", "grant", "uc_vault", "Agree to a grant over acme/web", "yes", "Agree",
                         "console", "pe_me", at)
        store.add_answer("grant:G8", "grant", "uc_vault", "Agree to a grant over acme/api", "yes", "Agree",
                         "console", "pe_else", at)
        ids = [r["item"] for r in store.list_answers(at - timedelta(seconds=1), hidden=["uc_vault"], own="pe_me")
               if r["item"] in ("milestone_to_check:uc_vault:m1", "grant:G9", "grant:G8")]
        ck("the caller's grant stays, their other answers and other people's grants do not",
           ids == ["grant:G9"], ids)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
