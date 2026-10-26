"""You in the loop (M7: TR-449..453).

One desk for every project (D1, D2... for the cell, blockers first), written only by the
orchestrator. The person answers in the orchestrator's session; the answer is kept word for word
and matched against what they typed there (a paraphrase is "not found"). A standing answer that
matches becomes a grant; one that does not, never. A batch of assumptions is kept or overturned in
one answer, an overturned one naming the tickets it touched. Decisions reach the ticket, the
pick-up and the reviewer's brief. A permission a station was refused is a desk item with the
station and the exact command; only the person, in the console, allows it: the grant is kept for
the project or every project, recorded as a decision made in the console, and the item answered.

Run: .venv/bin/python tests/test_factory_desk.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "claude-plugin", "scripts"))

_TMP = tempfile.mkdtemp(prefix="tares-factory-desk-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-desk"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import ship
import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import factory

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ago(minutes):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def unit():
    print("== words as typed ==")
    turns = [{"session": "o1", "text": "Hmm. Use Stripe test mode, and keep it simple!"},
             {"session": "o1", "text": "I don't want to ship it until Friday\nyes"}]
    ck("a whole sentence of theirs matches, case and spacing aside",
       factory.words_match("use  stripe test mode, and keep it simple", turns) is not None)
    ck("a whole line matches", factory.words_match("Yes", turns) is not None)
    ck("a fragment does not", factory.words_match("use stripe test mode", turns) is None
       and factory.words_match("ship it", turns) is None)
    ck("nor a word hidden in another", factory.words_match("ok", [{"text": "token"}]) is None)
    ck("a paraphrase is not", factory.words_match("Go with Stripe", turns) is None)
    ck("one character is not an answer", factory.words_match("y", turns) is None)
    print("== a long pasted reply (TR-697) ==")
    lines = [f"A{n}: line {n} of the reply, " + "word " * 25 for n in (2, 3, 5, 6, 7, 10)]
    pasted = '\n\n<pasted_content id="964c">\nKeep A1, A4. Change the others as below.\n' \
        + "\n".join(lines) + '\n</pasted_content id="964c">\n'
    pt = [{"session": "o1", "text": pasted}]
    ck("the paste is long enough to matter", len(pasted) > 1000, len(pasted))
    ck("every line of it matches, the last too",
       all(factory.words_match(x, pt) is not None for x in lines))
    ck("the first line matches without the tag", factory.words_match("Keep A1, A4.", pt) is not None)
    ck("the whole paste matches without its tags",
       factory.words_match(pasted.split(">", 1)[1].rsplit("</", 1)[0], pt) is not None)
    ck("and with them", factory.words_match(pasted, pt) is not None)
    ck("a tag alone is not an answer", factory.words_match('<pasted_content id="964c">', pt) is None)
    glued = [{"session": "o1", "text": 'abc<pasted_content id="1">foo</pasted_content id="1">'}]
    ck("a tag does not glue its neighbours into a word they never typed",
       factory.words_match("abcfoo", glued) is None and factory.words_match("foo", glued) is not None)
    print("== an option's letter (TR-475) ==")
    ck("B, b, B), (B) and B. name the second option",
       [factory.option_letter(w) for w in ("B", "b", "B)", "(B)", "B.", " b ")] == [1] * 6)
    ck("words are not a letter", [factory.option_letter(w) for w in ("ok", "B but later", "")]
       == [None] * 3)
    lt = [{"session": "o1", "text": "maybe B"}, {"session": "o1", "text": "B but later"}]
    ck("a letter inside a longer message is not the letter", factory.letter_match(1, lt) is None)
    ck("a message that is the letter alone is",
       factory.letter_match(1, lt + [{"session": "o1", "text": "b)"}]) is not None)
    ck("every project only when they say so", factory.says_every_project("fine for every project")
       and not factory.says_every_project("fine for staging"))


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

    async def ingest(lines):
        async with mk() as cx:
            await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        blog = json.loads(await m.create_project("Blog", "Write"))["id"]
        async with mk() as cx:
            await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}, "project": shop})
        for t in ("Catalog", "Payments", "Checkout"):
            await m.write_ticket(t, project="Shop")
        await m.write_ticket("Post", project="Blog")

        print("== one desk (TR-449) ==")
        as_station("crew-orchestrator", "orchestrator")
        d1 = json.loads(await mf.desk_add("Payments: Stripe test mode or Paddle?", "Stripe",
                                          "the spec mentions EU cards and it has a test mode",
                                          ["Stripe", "Paddle"], blocks="shop-build-m1 on T2",
                                          tickets=["T2"], blocking=True, project="Shop"))
        d2 = json.loads(await mf.desk_add("Blog: comments on or off?", "off", "less moderation",
                                          project="Blog"))
        d3 = json.loads(await mf.desk_add("Staging deploys: ask each time?", "no, standing yes",
                                          "they are cheap to undo", project="Shop"))
        ck("one numbering for the cell", [d1["label"], d2["label"], d3["label"]] == ["D1", "D2", "D3"],
           (d1, d2, d3))
        lst = json.loads(await mf.desk_list())
        ck("blockers first, then oldest", [d["label"] for d in lst] == ["D1", "D2", "D3"], lst)
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        bad = await mf.desk_add("x?", "y", "z", project="Shop")
        ck("a builder cannot write the desk", "only the orchestrator" in bad, bad)
        bad = await mf.desk_answer("D1", "Stripe")
        ck("nor answer it", "only the orchestrator" in bad, bad)

        print("== answers word for word (TR-450) ==")
        factory.MATCH_WAIT_SECONDS = 0   # the misses below answer at once; TR-475 sets its own
        def now_plus(seconds):
            t = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        await ingest([
            {"type": "session_station", "sessionId": "o1", "cwd": "/w", "timestamp": ago(10),
             "station": "crew-orchestrator", "role": "orchestrator"},
            {"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": ago(30),
             "message": {"role": "user", "content": "Paddle, obviously"}},
            {"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(1),
             "message": {"role": "user", "content": "Use Stripe test mode for now"}},
            {"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(2),
             "message": {"role": "user", "content": "Always fine to deploy to staging"}},
            {"type": "user", "sessionId": "x1", "cwd": "/w", "timestamp": now_plus(2),
             "message": {"role": "user", "content": "Comments off please"}},
        ])
        as_station("crew-orchestrator", "orchestrator")
        out = json.loads(await mf.desk_answer("D1", "Paddle, obviously"))
        ck("words typed before the question was asked do not answer it", out["recorded"] is False, out)
        out = json.loads(await mf.desk_answer("D1", "Use Stripe test mode for now", choice="Stripe"))
        ck("an answer copied from the session matches", out["matches_what_they_typed"], out)
        g = json.loads(await m.get_ticket("T2", project="Shop"))
        ck("the decision reaches its ticket, first", g["decisions"] and g["decisions"][0]["words"]
           == "Use Stripe test mode for now", g.get("decisions"))
        out = json.loads(await mf.desk_answer("D2", "Comments off please"))
        ck("words typed in another session, not the orchestrator's: nothing recorded, still open",
           out["recorded"] is False and out["still_open"]
           and "D2" in [d["label"] for d in json.loads(await mf.desk_list())], out)
        bad = await mf.desk_answer("D1", "again")
        ck("an answered item is closed", "already answered" in bad, bad)
        bad = await mf.desk_answer("D3", "Always fine to deploy to staging", standing=True, scope="all")
        ck("a project's question does not become a grant for every project unless they say so",
           "only when the person says so" in bad, bad)

        print("== a letter answers, and a reply not read yet is waited for (TR-475) ==")
        dl = json.loads(await mf.desk_add("Stop the crew?", "yes", "no project left",
                                          ["yes", "no"], project="Shop"))["label"]
        await ingest([{"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(1),
                       "message": {"role": "user", "content": "maybe B"}}])
        out = json.loads(await mf.desk_answer(dl, "B"))
        ck("B inside a longer message does not answer", out["recorded"] is False, out)
        bad = await mf.desk_answer(dl, "C")
        ck("a letter the item has no option for is refused", "not one of" in bad
           and "A) yes, B) no" in bad, bad)
        bad = await mf.desk_answer(dl, "B", choice="yes")
        ck("a choice that disagrees with the letter is refused", "B) no, not 'yes'" in bad, bad)
        await ingest([{"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(2),
                       "message": {"role": "user", "content": "B"}}])
        out = json.loads(await mf.desk_answer(dl, "B", choice="no"))
        ck("the letter alone answers, recorded as its option", out["recorded"]
           and out["choice"] == "no", out)
        async with mk() as cx:
            decs = (await cx.get(f"/api/projects/{shop}/decisions")).json()
        decs = decs.get("decisions", decs) if isinstance(decs, dict) else decs
        dec = next((x for x in decs if x.get("words") == "B"), None)
        ck("the decision keeps what they typed and the option it means",
           dec is not None and dec["choice"] == "no", decs)
        dw = json.loads(await mf.desk_add("Ship on Friday?", "yes", "it is ready",
                                          project="Shop"))["label"]
        factory.MATCH_WAIT_SECONDS = 6

        async def typed_late():   # the person's reply reaches Tares 2 s after the call
            await asyncio.sleep(2)
            await ingest([{"type": "user", "sessionId": "o1", "cwd": "/w",
                           "timestamp": now_plus(3),
                           "message": {"role": "user", "content": "Friday is fine"}}])
        t0 = asyncio.get_running_loop().time()
        late, out = await asyncio.gather(typed_late(), mf.desk_answer(dw, "Friday is fine"))
        out = json.loads(out)
        took = asyncio.get_running_loop().time() - t0
        ck("a reply Tares reads only after the call is still recorded on the first call",
           out["recorded"] and took < 6, (out, took))
        factory.MATCH_WAIT_SECONDS = 2
        dm = json.loads(await mf.desk_add("Rename the shop?", "no", "nobody asked",
                                          project="Shop"))["label"]
        t0 = asyncio.get_running_loop().time()
        out = json.loads(await mf.desk_answer(dm, "something nobody typed"))
        took = asyncio.get_running_loop().time() - t0
        ck("a miss gives up after the wait", out["recorded"] is False and 1.5 < took < 5,
           (out, took))
        factory.MATCH_WAIT_SECONDS = 0

        print("== standing answers become grants (TR-451) ==")
        out = json.loads(await mf.desk_answer("D3", "Always fine to deploy to staging", standing=True))
        ck("a standing answer that matches becomes a grant", out["became_a_grant"], out)
        txt = await mf.read_grants(project="Shop")
        ck("in the project's grants, quoted and dated",
           '"Always fine to deploy to staging" (20' in txt, txt)
        d4 = json.loads(await mf.desk_add("Prod deploys?", "ask me", "risky", project="Shop"))
        out = json.loads(await mf.desk_answer(d4["label"], "You may always deploy to prod", standing=True))
        ck("a standing answer the person did not type never becomes a grant",
           out["recorded"] is False, out)
        ck("and is not in the grants", "deploy to prod" not in await mf.read_grants(project="Shop"))

        print("== assumption batches (TR-452) ==")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        for q, c, t in (("Currency?", "EUR", "T1"), ("Page size?", "20", "T1"),
                        ("Date format?", "ISO", "T3"), ("Rounding?", "half up", "T2")):
            await mf.assume(q, c, "default", [t], project="Shop")
        as_station("crew-orchestrator", "orchestrator")
        d5 = json.loads(await mf.desk_add("Keep these assumptions?", "keep all four", "they are defaults",
                                          assumptions=["A1", "A2", "A3", "A4"], project="Shop"))
        ck("a batch carries its assumptions", d5["assumptions"] == ["A1", "A2", "A3", "A4"], d5)
        await ingest([{"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(3),
                       "message": {"role": "user", "content": "keep 1, 2, 4; overturn 3, use the other date format"}}])
        bad = await mf.desk_answer(d5["label"], "keep 1, 2, 4; overturn 3, use the other date format",
                                   kept=["A1", "A2"], overturned={"A3": "use the other date format"})
        ck("a batch answer decides every listed assumption", "left out: A4" in bad, bad)
        out = json.loads(await mf.desk_answer(d5["label"], "keep 1, 2, 4; overturn 3, use the other date format",
                                              kept=["A1", "A2", "A4"],
                                              overturned={"A3": "use the other date format"}))
        ck("three kept, one overturned", sorted((a["label"], a["state"]) for a in out["assumptions"])
           == [("A1", "kept"), ("A2", "kept"), ("A3", "overturned"), ("A4", "kept")], out)
        ck("the overturned one names the tickets it touched", out["follow_up_tickets_needed"]
           == [{"assumption": "A3", "tickets": ["T3"]}], out)
        asm = json.loads(await mf.list_assumptions(project="Shop"))
        a3 = next(a for a in asm if a["label"] == "A3")
        ck("with the person's words", a3["words"] == "use the other date format" and a3["state"] == "overturned", a3)

        print("== decisions travel (TR-451) ==")
        async with mk() as cx:
            pk = (await cx.get(f"/api/projects/{shop}/pickup")).json()
            br = (await cx.get(f"/api/projects/{shop}/tickets/T2/brief")).json()["brief"]
        ck("the pick-up has them", any(d["words"] == "Use Stripe test mode for now" for d in pk["decisions"]), pk["decisions"])
        ck("so does the reviewer's brief", "Use Stripe test mode for now" in br, br[-600:])
        dl = json.loads(await mf.list_decisions(project="Shop"))
        ck("list_decisions, newest first, only what the person typed",
           [d["label"] for d in dl] == ["D8", "D3", "D5", "D4", "D1"] and all(d["matched"] for d in dl), dl)

        print("== withdrawing ==")
        d6 = json.loads(await mf.desk_add("Logo colour?", "blue", "brand", project="Blog"))
        out = json.loads(await mf.desk_withdraw(d6["label"], "the designer decided"))
        ck("a withdrawn item leaves the open desk", out["state"] == "withdrawn"
           and d6["label"] not in [d["label"] for d in json.loads(await mf.desk_list())], out)
        as_station()
        out = json.loads(await mf.desk_add("A person's own question?", "yes", "testing", project="Blog"))
        ck("a person's own session may use the desk", out["label"] == "D10", out)

        print("== finishing a project, on the person's word ==")
        async with mk() as cx:
            await cx.post(f"/api/projects/{blog}/handover", json={"repos": [{"path": "/w/blog"}]})
            await cx.post(f"/api/projects/{shop}/handover", json={"repos": [{"path": "/w/shop"}]})
        as_station("crew-orchestrator", "orchestrator")
        bad = await mf.finish_project("Blog", "D2")
        ck("not while a ticket is open", "still has open tickets: T1" in bad, bad)
        as_station()   # the person's own session moves the stage here
        async with mk() as cx:
            await cx.post(f"/api/projects/{blog}/tickets/T1/stage", json={"stage": "doing"})
            await cx.post(f"/api/projects/{blog}/tickets/T1/stage", json={"stage": "merged"})
        as_station("crew-orchestrator", "orchestrator")
        moot = json.loads(await mf.desk_add("Blog banner colour?", "red", "brand", project="Blog"))
        d8 = json.loads(await mf.desk_add("Is Blog done?", "yes, it's done", "every ticket merged",
                                          ["yes, it's done", "no, there is more"], project="Blog"))
        bad = await mf.finish_project("Blog", d8["label"])
        ck("not before the person answered", "has no answer from the person yet" in bad, bad)
        bad = await mf.finish_project("Blog", "D1")
        ck("not on a question about another project", "another project" in bad, bad)
        await ingest([{"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(5),
                       "message": {"role": "user", "content": "yes, the blog is done"}}])
        json.loads(await mf.desk_answer(d8["label"], "yes, the blog is done"))
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        bad = await mf.finish_project("Blog", d8["label"])
        ck("a builder may not finish a project", "only the orchestrator" in bad, bad)
        as_station("crew-orchestrator", "orchestrator")
        out = json.loads(await mf.finish_project("Blog", d8["label"]))
        ck("finished in the person's words, with the desk item", out["finished"]["words"]
           == "yes, the blog is done" and out["finished"]["decision"] == d8["label"], out)
        ck("it says how many projects the crew still holds", out["open_projects"] == 1, out)
        async with mk() as cx:
            left = {d["label"]: d for d in
                    (await cx.get("/api/desk", params={"state": "withdrawn"})).json()["desk"]}
        ck("its open questions are withdrawn with it", moot["label"] in left
           and left[moot["label"]]["reason"] == "the project is finished"
           and left[moot["label"]]["project_finished"], left.get(moot["label"]))
        async with mk() as cx:
            ps = (await cx.get("/api/crew/projects")).json()
            plist = (await cx.get("/api/projects")).json()
            st = next(p for p in (plist.get("projects", []) if isinstance(plist, dict) else plist)
                      if p["id"] == blog)
        ck("off the crew's work list", [p["name"] for p in ps["projects"]] == ["Shop"], ps["projects"])
        ck("listed as finished", [p["name"] for p in ps["finished"]] == ["Blog"], ps.get("finished"))
        ck("the project is done", st["status"] == "done", st)
        bad = await mf.finish_project("Blog", d8["label"])
        ck("finishing twice is refused", "already finished" in bad, bad)
        async with mk() as cx:
            await cx.post(f"/api/projects/{blog}/handover", json={"repos": [{"path": "/w/blog"}]})
            ps = (await cx.get("/api/crew/projects")).json()
            ho = (await cx.get(f"/api/projects/{blog}/crew")).json()["handover"]
        ck("a new hand-over opens it again", "Blog" in [p["name"] for p in ps["projects"]]
           and ho["finished"] is None, ps)
        bad = await mf.finish_project("Blog", "")
        ck("a station must name the desk item", "name the desk item" in bad, bad)
        as_station()
        async with mk() as cx:
            r = await cx.post(f"/api/projects/{blog}/finish", json={"words": "shipped, closing it"})
        ck("the person's own session finishes it in their words", r.status_code == 200
           and r.json()["finished"]["words"] == "shipped, closing it"
           and r.json()["finished"]["decision"] is None, r.text)

        print("== a permission a station was refused, allowed in the console ==")
        as_station("crew-orchestrator", "orchestrator")
        cmd = "gh pr merge 12 --squash  --repo acme/shop"
        pq = json.loads(await mf.desk_add(
            "shop-build-m1 was refused a command. Allow it?",
            "shop-build-m1 may run `gh pr merge 12 --squash` in Shop",
            "it merges its own pull request once the review passed", blocking=True,
            project="Shop", station="shop-build-m1", command=f"  {cmd}\n"))
        ck("a permission request keeps the station and the command as it was refused",
           pq["station"] == "shop-build-m1" and pq["command"] == cmd, pq)
        async with mk() as cx:
            row = next(d for d in (await cx.get(f"/api/desk?project={shop}")).json()["desk"]
                       if d["label"] == pq["label"])
        ck("and the desk serves them", row["station"] == "shop-build-m1"
           and row["command"] == cmd and row["project_name"] == "Shop", row)
        bad = await mf.desk_add("x?", "y", "z", project="Shop", command="ls")
        ck("a command needs the station that was refused it", "names the station" in bad, bad)
        bad = await mf.desk_add("x?", "y", "z", project="Shop", station="s", command="x" * 2001)
        ck("a command is kept to 2000 characters", "keep it to 2000" in bad, bad)
        bad = await mf.desk_add("x?", "y", "z", project="Shop", station="s" * 121, command="ls")
        ck("a station name to 120", "keep it to 120" in bad, bad)
        plain = json.loads(await mf.desk_add("Dark mode?", "later", "not in the plan",
                                             project="Shop"))
        ck("a question is not a permission request", plain["command"] is None, plain)
        crewq = json.loads(await mf.desk_add(
            "crew-reviewer was refused a command. Allow it?",
            "crew-reviewer may run `gh api repos/acme/shop/statuses/abc` in every project",
            "it sets the review status", station="crew-reviewer",
            command="gh api repos/acme/shop/statuses/abc -f state=success"))

        async def allow(label, body):
            async with mk() as cx:
                return await cx.post(f"/api/desk/{label}/allow", json=body)
        words = "shop-build-m1 may run `gh pr merge 12 --squash` in Shop"
        r = await allow(pq["label"], {"words": words, "scope": "project"})
        ck("the orchestrator cannot allow it", r.status_code == 403 and "console" in r.text, r.text)
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        r = await allow(pq["label"], {"words": words, "scope": "project"})
        ck("nor the station that wants it", r.status_code == 403, r.text)
        as_station()
        r = await allow(plain["label"], {"words": "fine", "scope": "project"})
        ck("an item with no command is answered in the session, not allowed",
           r.status_code == 409 and "not a permission request" in r.text, r.text)
        r = await allow(pq["label"], {"words": "  ", "scope": "project"})
        ck("allowing needs the words of the grant", r.status_code == 400, r.text)
        r = await allow(pq["label"], {"words": "x" * 401, "scope": "project"})
        ck("kept to 400 characters, as every grant", r.status_code == 400
           and "keep it to 400" in r.text, r.text)
        r = await allow(pq["label"], {"words": words, "scope": "everywhere"})
        ck("scope is project or all", r.status_code == 400, r.text)
        r = await allow(crewq["label"], {"words": "x", "scope": "project"})
        ck("an item with no project is allowed only for every project", r.status_code == 400
           and "every project" in r.text, r.text)
        as_station("crew-orchestrator", "orchestrator")
        crewp = json.loads(await mf.desk_add(
            "crew-reviewer was refused a command. Allow it?", "crew-reviewer may comment on PR 12",
            "it reviews Shop's pull request", project="Shop", station="crew-reviewer",
            command="gh pr comment 12 --body ok"))
        as_station()
        r = await allow(crewp["label"], {"words": "crew-reviewer may comment", "scope": "project"})
        ck("a crew station's grant is for every project, even asked on one (it starts with only "
           "those)", r.status_code == 400 and "never reaches it" in r.text, r.text)
        r = await allow(pq["label"], {"words": words, "scope": "project"})
        out = r.json()
        ck("the console allows it for the station, with the grant line", r.status_code == 200
           and out["station"] == "shop-build-m1" and out["added"]
           and out["line"].startswith(f'"{words}" (20'), r.text)
        async with mk() as cx:
            g = (await cx.get("/api/grants", params={"project": shop})).json()
        ck("into the project's grants", f'"{words}" (20' in g["project"], g)
        ck("not the grants for every project", words not in g["all"], g)
        ck("the item is answered", out["desk_item"]["state"] == "answered"
           and pq["label"] not in [d["label"] for d in json.loads(await mf.desk_list())], out)
        dec = out["decision"]
        ck("recorded as the person's decision, made in the console", dec["via"] == "console"
           and dec["words"] == words and dec["standing"] and dec["granted"]
           and not dec["matched"] and dec["scope"] == "project" and dec["label"] == pq["label"], dec)
        dl = json.loads(await mf.list_decisions(project="Shop"))
        ck("list_decisions says so", dl[0]["label"] == pq["label"] and dl[0]["via"] == "console", dl[0])
        r = await allow(pq["label"], {"words": words, "scope": "project"})
        ck("allowing twice is refused", r.status_code == 409 and "already answered" in r.text, r.text)
        cw = "crew-reviewer may set the review status on this crew's pull requests"
        r = await allow(crewq["label"], {"words": f'"{cw}"', "scope": "all"})
        ck("a crew-wide station's grant goes to every project", r.status_code == 200
           and f'"{cw}" (20' in await mf.read_grants()
           and r.json()["decision"]["scope"] == "all", r.text)

        print("== a reply typed while the orchestrator's turn runs (TR-522) ==")
        factory.MATCH_WAIT_SECONDS = 0
        as_station("crew-orchestrator", "orchestrator")
        dq = json.loads(await mf.desk_add("Can the releaser read prod?", "yes, reads only",
                                          "it checks the deploy", project="Shop"))

        def queued(text, mode="prompt", kind="human"):
            at = (datetime.now(timezone.utc) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            return json.dumps({"type": "attachment", "sessionId": "o1", "cwd": "/w", "timestamp": at,
                               "uuid": f"q-{len(text)}-{kind}",
                               "attachment": {"type": "queued_command", "prompt": text,
                                              "commandMode": mode, "origin": {"kind": kind},
                                              "timestamp": at}})
        raw = [queued("yeah releaser can do reads on prod thats fine"),
               queued("<cross-session-message>reads on prod are fine</cross-session-message>", kind="peer"),
               queued("<task-notification>done</task-notification>", mode="task-notification",
                      kind="task-notification"),
               json.dumps({"type": "queue-operation", "sessionId": "o1", "operation": "enqueue"})]
        objs, _flow, _changed = ship.process_lines(raw, "", {})
        ck("only the person's queued message is shipped, as a user line",
           [(o["type"], o.get("queued")) for o in objs] == [("user", True)]
           and objs[0]["message"]["content"] == "yeah releaser can do reads on prod thats fine", objs)
        await ingest(objs)
        out = json.loads(await mf.desk_answer(dq["label"], "reads on prod are fine"))
        ck("words another session sent do not answer", out["recorded"] is False, out)
        out = json.loads(await mf.desk_answer(dq["label"], "yeah releaser can do reads on prod thats fine"))
        ck("a reply typed mid-turn answers, word for word",
           out["recorded"] and out["matches_what_they_typed"], out)

        print("== a long pasted reply is kept whole (TR-697) ==")
        dp = json.loads(await mf.desk_add("Which of these?", "A2", "default", project="Shop"))["label"]
        rows = [f"A{n}: line {n} of the reply, " + "word " * 30 for n in (2, 3, 5, 6, 7, 10)]
        paste = '<pasted_content id="964c">\nKeep A1, A4.\n' + "\n".join(rows) \
            + '\n</pasted_content id="964c">\n'
        await ingest([{"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(5),
                       "message": {"role": "user", "content": paste}}])
        out = json.loads(await mf.desk_answer(dp, rows[-1]))
        ck("the last line of a paste over 1000 characters answers", len(paste) > 1000
           and out["recorded"] and out["matches_what_they_typed"], (len(paste), out))
        print("== lines Claude Code writes for itself are not what they typed (TR-697) ==")
        for n, flag in enumerate(("isCompactSummary", "isMeta")):
            words = f"Go with Adyen for the {flag} line, said a peer"
            dm = json.loads(await mf.desk_add(f"Which gateway {n}?", "Adyen", "default",
                                              project="Shop"))["label"]
            body = ("This session is being continued from a previous conversation. " * 12
                    + "\n" + words + "\n")
            await ingest([{"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(6 + n),
                           flag: True, "message": {"role": "user", "content": body}}])
            out = json.loads(await mf.desk_answer(dm, words))
            ck(f"a {flag} line, even a long one, does not answer", len(body) > 600
               and out["recorded"] is False, (len(body), out))


unit()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
