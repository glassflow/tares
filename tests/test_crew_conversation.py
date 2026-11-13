"""A station's session as a conversation (TR-1177), through the claude_code ingest and GET /api/crew/{name}.

A recorded session: a message from the orchestrator, one typed by a person, the factory's own tick, text the
station said, tool calls whose results come back out of order, a failed call, an output longer than the
list holds, a skill's text and a task notification the session gives itself, and a compaction.

  - items come oldest first; each tool card holds its own result, matched by call id, not by position;
  - a result marked is_error is a failed card;
  - who sent each message: the station named in the message, a person, the factory (quiet);
  - lines the session gives itself are not messages;
  - a long output is cut in the list, `more` says so, and GET /api/crew/{name}/tool/{id} gives it whole;
  - `before` reads the lines before an earlier answer's `conversation_from` (Show earlier);
  - `recent` is still there for the callers that read it.

Run: .venv/bin/python tests/test_crew_conversation.py
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

_TMP = tempfile.mkdtemp(prefix="tares-crew-convo-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-convo"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import crew_conversation as cc

P = F = 0
SID = "b1"
ST = "shop-build-m1"
LONG = "line of test output\n" * 400   # 8000 characters: more than the list holds


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(seconds_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def line(kind, ago, **kw):
    return {"type": kind, "sessionId": SID, "cwd": "/w/shop", "timestamp": ts(ago), "factory_station": ST, **kw}


def user_text(text, ago, **kw):
    return line("user", ago, message={"role": "user", "content": text}, **kw)


def said(ago, text, tools=()):
    content = ([{"type": "text", "text": text}] if text else []) + [
        {"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in tools]
    return line("assistant", ago, message={"role": "assistant", "content": content})


def results(ago, *rs):
    return line("user", ago, message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": i, "content": out, "is_error": err} for i, out, err in rs]})


SESSION = [
    {"type": "session_station", "sessionId": SID, "cwd": "/w/shop", "timestamp": ts(600), "station": ST,
     "role": "builder", "parent": "crew-orchestrator", "factory_station": ST},
    user_text('Another Claude session sent a message:\n<cross-session-message from="uds:/tmp/cc-socks/1.sock" '
              'from-name="crew-orchestrator" from-mode="prompting">Take T1: add the search box.</cross-session-message>',
              590, isMeta=True, turnOrigin="peer", promptSource="system"),
    user_text("Base directory for this skill: /x/skills/station-builder", 589, isMeta=True),
    said(580, "Reading the page first.", [("t-read", "Read", {"file_path": "src/page.tsx"}),
                                          ("t-test", "Bash", {"command": "npm test", "timeout": 600000})]),
    # the results come back in the other order
    results(570, ("t-test", "2 failed, 10 passed", True), ("t-read", "export function Page() {}", False)),
    said(560, "", [("t-grep", "Grep", {"pattern": "SearchBox"}), ("t-log", "Bash", {"command": "cat build.log"})]),
    results(555, ("t-grep", "no matches", False)),
    results(550, ("t-log", LONG, False)),
    user_text("<task-notification>\n<task-id>x</task-id>\n</task-notification>", 545, promptSource="system"),
    user_text("please also check the mobile layout", 540, promptSource="typed", turnOrigin="human"),
    user_text("Builder tick: follow the Tick section of the station-builder skill.", 530),
    line("system", 520, subtype="compact_boundary", content="Conversation compacted"),
    said(510, "Done. Opened #12."),
]


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    mk = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", headers=root)
    async with app.router.lifespan_context(app):
        async with mk() as cx:
            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            ck("the claude_code source is made", r.status_code < 300, r.text)
            r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in SESSION) + "\n",
                              headers={"Content-Type": "application/x-ndjson"})
            ck("the session is taken in", r.status_code < 300, r.text)
            d = (await cx.get(f"/api/crew/{ST}", params={"conversation": 200})).json()
            plain = (await cx.get(f"/api/crew/{ST}")).json()
        items = d.get("conversation") or []
        kinds = [i["kind"] for i in items]
        ck("the answer has the conversation, oldest first",
           kinds[:2] == ["divider", "asked"] and items[-1]["kind"] == "said" and items[-1]["text"] == "Done. Opened #12.",
           kinds)
        ck("without asking for it, the answer is what it was: recent, and no conversation",
           "conversation" not in plain and isinstance(plain.get("recent"), list) and plain["recent"])
        ck("the session's start is a divider", items[0]["text"] == "Session started", items[0])
        tools = {i["id"]: i for i in items if i["kind"] == "tool"}
        ck("each call is its own card, in the order it was made",
           [i["id"] for i in items if i["kind"] == "tool"] == ["t-read", "t-test", "t-grep", "t-log"], list(tools))
        ck("each card holds its own result, matched by call id, not by position",
           tools["t-read"]["output"] == "export function Page() {}" and tools["t-test"]["output"] == "2 failed, 10 passed"
           and tools["t-grep"]["output"] == "no matches", tools)
        ck("a result marked as an error is a failed card",
           tools["t-test"]["failed"] is True and tools["t-read"]["failed"] is False)
        ck("a card has the tool's name, a one-line summary and its input as JSON",
           tools["t-test"]["name"] == "Bash" and tools["t-test"]["summary"] == "npm test"
           and json.loads(tools["t-test"]["input"]) == {"command": "npm test", "timeout": 600000}
           and tools["t-read"]["summary"] == "src/page.tsx", tools["t-test"])
        ck("and how long it took", tools["t-read"]["ms"] is not None and 9000 <= tools["t-read"]["ms"] <= 11000,
           tools["t-read"]["ms"])
        ck("a long output is cut in the list and says so",
           len(tools["t-log"]["output"]) == cc.CAP and tools["t-log"]["more"] is True
           and tools["t-read"]["more"] is False)
        asked = [i for i in items if i["kind"] == "asked"]
        ck("a message from another station names it, without the wrapper",
           asked[0]["from"] == "crew-orchestrator" and asked[0]["text"] == "Take T1: add the search box."
           and asked[0]["quiet"] is False, asked[0])
        ck("a prompt a person typed is theirs", any(a["from"] == cc.PERSON and "mobile layout" in a["text"]
                                                    for a in asked), asked)
        ck("the factory's own tick is the factory, quiet",
           any(a["from"] == cc.FACTORY and a["quiet"] and "tick" in a["text"] for a in asked), asked)
        ck("a skill's text and a task notification are not messages",
           not any("Base directory" in a["text"] or "task-notification" in a["text"] for a in asked), asked)
        ck("a compaction is a divider", any(i["kind"] == "divider" and i["text"] == "Memory compacted" for i in items))
        said = [i for i in items if i["kind"] == "said"]
        ck("what the station said is its own item", [s["text"] for s in said] == ["Reading the page first.", "Done. Opened #12."])

        async with mk() as cx:
            r = await cx.get(f"/api/crew/{ST}/tool/t-log")
            ck("the full output of one call loads on its own", r.status_code == 200 and r.json()["output"] == LONG
               and r.json()["failed"] is False, r.text[:200])
            r = await cx.get(f"/api/crew/{ST}/tool/t-test")
            ck("and says when it failed", r.json()["failed"] is True)
            r = await cx.get(f"/api/crew/{ST}/tool/nope")
            ck("an unknown call is 404", r.status_code == 404)

            last = (await cx.get(f"/api/crew/{ST}", params={"conversation": 4})).json()
            ck("a short read gives the last lines, and where it starts",
               last["conversation"][-1]["text"] == "Done. Opened #12." and last["conversation_from"] > 0, last.get("conversation_from"))
            earlier = (await cx.get(f"/api/crew/{ST}", params={"conversation": 200,
                                                                "before": last["conversation_from"]})).json()
            ck("Show earlier reads the lines before that point, from the start",
               earlier["conversation_from"] == 0 and earlier["conversation"][0]["text"] == "Session started"
               and not any(i.get("text") == "Done. Opened #12." for i in earlier["conversation"]), earlier.get("conversation_from"))

        async with mk() as cx:
            whole = (await cx.get(f"/api/crew/{ST}", params={"conversation": 200})).json()
            tail = (await cx.get(f"/api/crew/{ST}", params={"conversation": 200, "since": 5})).json()
            ck("since reads from that line to the newest, whatever the window, and says where it starts",
               tail["conversation_from"] == 5 and tail["conversation"][-1]["text"] == "Done. Opened #12."
               and len(tail["conversation"]) < len(whole["conversation"]), tail.get("conversation_from"))
            beyond = (await cx.get(f"/api/crew/{ST}", params={"conversation": 200, "since": 999})).json()
            ck("a since past the newest line reads nothing new", beyond["conversation"] == [], beyond.get("conversation"))

    # TR-1317: the header line of the most common calls says what the call did
    ck("a Bash call's summary is its description, on one line, before the command",
       cc.summary("Bash", {"command": "uv run pytest -q", "description": "Run the\n  parser tests"}) == "Run the parser tests")
    ck("a Bash call with no description still shows its command",
       cc.summary("Bash", {"command": "npm test", "description": "  "}) == "npm test")
    todos = [{"content": "Read", "status": "completed", "activeForm": "Reading"},
             {"content": "Plan", "status": "completed", "activeForm": "Planning"},
             {"content": "Build the Bash view", "status": "in_progress", "activeForm": "Building the Bash view"},
             {"content": "Fall back", "status": "pending", "activeForm": "Falling back"},
             {"content": "Test", "status": "pending", "activeForm": "Testing"}]
    ck("a TodoWrite call counts the steps done and names the one in hand",
       cc.summary("TodoWrite", {"todos": todos}) == "2 of 5 done · Building the Bash view",
       cc.summary("TodoWrite", {"todos": todos}))
    ck("with no step in hand it only counts",
       cc.summary("TodoWrite", {"todos": todos[:2]}) == "2 of 2 done")
    ck("an empty plan falls back to the start of the input",
       cc.summary("TodoWrite", {"todos": []}) == '{"todos": []}')
    ck("an Agent call reads its description, not its brief",
       cc.summary("Agent", {"description": "Find the views", "subagent_type": "Explore", "prompt": "Search ui/src"})
       == "Find the views")
    ck("another tool keeps the first field that says most",
       cc.summary("Grep", {"pattern": "changeOf", "description": "x"}) == "changeOf")

    print(f"{P} passed, {F} failed")
    return F


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(main()) else 0)
