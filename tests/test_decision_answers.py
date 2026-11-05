"""Decisions read as the answer chosen (TR-1018).

A desk answer keeps the person's words exactly as typed (they are matched against the person's
recorded session) and the option it picks. Checked here: which option a reply picks (a letter
alone or after the desk label, a letter before a separator, the option's own words, the
orchestrator's choice); that an item with options never records a decision without the option
(free words with no choice are refused, naming the options); that a choice given as a letter or a
shortened option becomes the option's full text, while a longer refinement is kept; how a
decision reads ({answer, said}); and that the project page shows "merge main into teams" with
"You said: D85 A".

Run: .venv/bin/python tests/test_decision_answers.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-decision-answers-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-decision-answers"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PR_SYNC_SECONDS"] = "off"

import httpx

import tares.mcp_factory as mf
import tares.mcp_server as m
from tares import factory

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    if cond:
        P += 1
        print(f"  ok   {label}")
    else:
        F += 1
        print(f"  FAIL {label}  {detail}")


OPTS = ["merge main into teams", "rebase teams onto main", "wait until teams goes to main"]


def unit():
    print("== which option a reply picks ==")
    pick = lambda w, c="": factory.picked_option(w, c, OPTS)
    ck("a letter alone", pick("A") == OPTS[0] and pick("b") == OPTS[1] and pick("(C)") == OPTS[2])
    ck("a letter after the desk label", pick("D85 A") == OPTS[0] and pick("d85: c") == OPTS[2])
    ck("a letter before a separator", pick("B - i will do it next week") == OPTS[1]
       and pick("A) yes") == OPTS[0] and pick("c: later") == OPTS[2] and pick("a. fine") == OPTS[0])
    ck("the option's own words, whole or cut short", pick("rebase teams onto main") == OPTS[1]
       and pick("Rebase teams") == OPTS[1] and pick("merge main into teams, please") == OPTS[0])
    ck("a word that starts like an option but is a sentence is not a letter",
       pick("a good idea") is None and pick("both sound fine") is None)
    ck("a letter past the options picks nothing", pick("D") is None and pick("D85 H") is None)
    ck("words that name no option pick nothing", pick("maybe later") is None)
    ck("no options: nothing to pick", factory.picked_option("A", "", []) is None)
    ck("the orchestrator's choice counts too", pick("yes", "B") == OPTS[1])

    print("== how a decision reads ==")
    dw = factory.decision_words
    ck("a letter reads as the option, with the person's words under it",
       dw({"words": "D85 A", "choice": OPTS[0]}) == {"answer": OPTS[0], "said": "D85 A"})
    ck("words equal to the answer, with or without the label, are not repeated",
       dw({"words": "keep all", "choice": "keep all"})["said"] is None
       and dw({"words": "D89 keep all", "choice": "keep all"})["said"] is None)
    ck("more words than the option are kept under it",
       dw({"words": "B - i will do it next week", "choice": OPTS[1]})["said"] == "B - i will do it next week")
    ck("no choice: the words are the answer", dw({"words": "ship it on friday", "choice": None})
       == {"answer": "ship it on friday", "said": None})


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk

    def as_station(station="", role=""):
        headers.clear()
        headers.update(root)
        if station:
            headers.update({"X-Tares-Station": station, "X-Tares-Role": role})

    def now_plus(seconds):
        t = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    async def ingest(lines):
        async with mk() as cx:
            await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})

    async def say(text, at):
        await ingest([{"type": "user", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(at),
                       "message": {"role": "user", "content": text}}])

    async with app.router.lifespan_context(app):
        uid = json.loads(await m.create_project("Teams", "Share one cell"))["id"]
        async with mk() as cx:
            await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}, "project": uid})
        await ingest([{"type": "session_station", "sessionId": "o1", "cwd": "/w", "timestamp": now_plus(-60),
                       "station": "crew-orchestrator", "role": "orchestrator"}])
        as_station("crew-orchestrator", "orchestrator")

        async def ask(q, opts):
            return json.loads(await mf.desk_add(q, opts[0] if opts else "go", "why not", opts, project="Teams"))

        print("== recording ==")
        d1 = await ask("How should teams take in main?", OPTS)
        await say(f"{d1['label']} A", 2)
        r = json.loads(await mf.desk_answer(d1["label"], f"{d1['label']} A"))
        ck("'D.. A' records the option it picks as the choice", r.get("recorded") is not False, r)

        d2 = await ask("Which way next?", OPTS)
        await say("we should talk about it first", 3)
        refused = await mf.desk_answer(d2["label"], "we should talk about it first")
        ck("free words with no choice on a question with options are refused, naming the options",
           "options this answer picks" in refused and "A) merge main into teams" in refused, refused)
        r = json.loads(await mf.desk_answer(d2["label"], "we should talk about it first",
                                            choice="none of them: talk it through first"))
        ck("the person's own answer, given as the choice, is recorded", r.get("recorded") is not False, r)

        d3 = await ask("And after that?", OPTS)
        await say("rebase it please", 4)
        r = json.loads(await mf.desk_answer(d3["label"], "rebase it please", choice="B"))
        ck("a choice given as a letter is recorded", r.get("recorded") is not False, r)

        d4 = await ask("Keep the calls?", ["keep all", "name the ones to change"])
        await say("keep all but change A3", 5)
        r = json.loads(await mf.desk_answer(d4["label"], "keep all but change A3",
                                            choice="keep all but change A3: the lead decides"))
        ck("a longer refinement of an option is kept as given", r.get("recorded") is not False, r)

        d5 = await ask("Anything else?", [])
        await say("ship it on friday", 6)
        r = json.loads(await mf.desk_answer(d5["label"], "ship it on friday"))
        ck("a question with no options records the words", r.get("recorded") is not False, r)

        print("== how the page reads them ==")
        as_station()
        async with mk() as cx:
            ds = (await cx.get(f"/api/projects/{uid}/decisions")).json()["decisions"]
            page = (await cx.get(f"/api/factory/projects/{uid}")).json()
        choice = {d["desk_item_label"] if "desk_item_label" in d else d.get("label"): d["choice"] for d in ds}
        ck("the choices stored", choice.get(d1["label"]) == OPTS[0] and choice.get(d3["label"]) == OPTS[1]
           and choice.get(d4["label"]) == "keep all but change A3: the lead decides"
           and choice.get(d2["label"]) == "none of them: talk it through first", choice)
        by_q = {d["question"]: d for d in page["decisions"]}
        ck("the page: 'D.. A' reads as the option, with 'You said'",
           by_q["How should teams take in main?"]["answer"] == OPTS[0]
           and by_q["How should teams take in main?"]["said"] == f"{d1['label']} A", by_q)
        ck("the page: a question with no options reads as the words, with no 'You said'",
           by_q["Anything else?"]["answer"] == "ship it on friday" and by_q["Anything else?"]["said"] is None, by_q)
        out = json.loads(await mf.list_decisions("Teams"))
        ck("the MCP listing carries the answer", any(x.get("answer") == OPTS[0] for x in out), out)


unit()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
