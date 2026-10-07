"""Picking a build up where a session stopped.

A fresh session asks Tares for the hand-over: the ticket in progress with its working doc, the
session that was on it (chosen by when the ticket went in progress), what it said last and the
files it touched, the builders' notes and a checklist. Taking over marks the old session replaced;
an old session that may still be open (waiting, or active in the last minutes) needs take_over,
a crashed one (quiet for longer) does not. Also the MCP tool's wording and list_tickets' hint.

Run: .venv/bin/python tests/test_pickup.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="tares-pickup-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-pickup"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import tares.mcp_server as m

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes: float) -> str:
    """An ISO timestamp `minutes` from now (negative: ago), as Claude Code writes them."""
    t = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def said(sid, minutes, text):
    return {"type": "assistant", "sessionId": sid, "cwd": "/w/home", "timestamp": ts(minutes),
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def edit(sid, minutes, path):
    return {"type": "assistant", "sessionId": sid, "cwd": "/w/home", "timestamp": ts(minutes),
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"t{path}", "name": "Edit",
                 "input": {"file_path": path, "old_string": "a", "new_string": "b"}}]}}


def link(sid, minutes, project):
    return {"type": "session_project", "sessionId": sid, "cwd": "/w/home", "timestamp": ts(minutes),
            "tares_project": project}


async def main():
    from tares.daemon import make_app
    app = make_app()
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = "http://t"
    m._cx = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                                 base_url="http://t", headers=root)
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers=root)
        await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                            "poll": "10s", "config": {"push": True}})

        async def ingest(*lines):
            r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(o) for o in lines) + "\n",
                              headers={"Content-Type": "application/x-ndjson"})
            assert r.status_code == 202, r.text

        uid = json.loads(await m.create_project("Sitters"))["id"]
        b = f"/api/projects/{uid}"
        w1 = (await cx.post(f"{b}/docs", json={"kind": "working", "title": "W1",
                                                "body": "steps one"})).json()["id"]
        w2 = (await cx.post(f"{b}/docs", json={"kind": "working", "title": "W2",
                                                "body": "## Context\npage\n\n## Progress\n- nav and dialogs work\n- next: phone width"})).json()["id"]
        await cx.post(f"{b}/docs", json={"kind": "note", "title": "Note: lint fails on main",
                                         "body": "6 errors already on main; left alone."})
        t1 = (await cx.post(f"{b}/tickets", json={"title": "Schema", "working_doc": w1})).json()["id"]
        t2 = (await cx.post(f"{b}/tickets", json={"title": "Sitters page", "working_doc": w2})).json()["id"]

        print("== nothing in progress ==")
        r = (await cx.get(f"{b}/pickup")).json()
        ck("no ticket in progress: the next one, nothing to take over",
           r["in_progress"] is False and r["ticket"]["id"] == t1 and r["previous_session"] is None,
           r)
        out = await m.pick_up(project="Sitters")
        ck("the tool says there is nothing to pick up", out.startswith("Nothing to pick up"), out)

        print("== a build session stops on ticket 2 ==")
        # the spec session, long ago, then the build session
        await ingest(link("spec", -60, "Sitters"), said("spec", -59, "spec done"))
        await ingest(link("build", -6, "Sitters"), said("build", -6, "Starting ticket 1."),
                     edit("build", -5, "/w/home/db/schema.sql"))
        await cx.put(f"{b}/tickets/{t1}", json={"status": "done"})
        await cx.put(f"{b}/tickets/{t2}", json={"status": "in_progress"})
        await ingest(edit("build", 0.2, "/w/home/src/app/babysitters/page.tsx"),
                     said("build", 0.3, "Nav, page and dialogs work."),
                     said("build", 0.4, "I've paused: the safety check stopped answering."),
                     {"type": "session_state", "sessionId": "build", "state": "waiting",
                      "reason": "the turn ended; the session waits for input", "timestamp": ts(0.5)})
        r = (await cx.get(f"{b}/pickup")).json()
        p = r["previous_session"]
        ck("the ticket in progress, with its working doc and Progress section",
           r["in_progress"] and r["ticket"]["id"] == t2
           and "## Progress" in r["ticket"]["working_doc_body"], r["ticket"])
        ck("the session that was on it, not the spec session",
           p["session"] == "build" and p["state"] == "waiting", p)
        ck("what it said last, oldest first, at most three",
           [s["text"] for s in p["said_lately"]] == ["Starting ticket 1.", "Nav, page and dialogs work.",
                                                    "I've paused: the safety check stopped answering."],
           p["said_lately"])
        ck("the files it touched, most recent first",
           p["files_touched"] == ["/w/home/src/app/babysitters/page.tsx", "/w/home/db/schema.sql"],
           p["files_touched"])
        ck("the builders' notes", [n["title"] for n in r["notes"]] == ["Note: lint fails on main"]
           and "6 errors" in r["notes"][0]["body"], r["notes"])
        ck("a checklist that starts with git", "git status" in r["checklist"][0], r["checklist"])
        ck("a waiting session may still be open", r["may_be_open"] is True)

        print("== taking over ==")
        r = await cx.post(f"{b}/pickup", json={})
        ck("not without take_over while it may be open -> 409", r.status_code == 409
           and "may still be open" in r.json()["detail"], r.text)
        out = await m.pick_up(project="Sitters")
        ck("the tool stops and says to ask the person", out.startswith("STOP")
           and "take_over=true" in out and "Sitters page" in out, out[:300])
        tl = json.loads(await m.list_tickets(project="Sitters"))
        ck("list_tickets says a ticket is in progress", "pick_up" in tl.get("note", ""), tl)
        out = await m.pick_up(project="Sitters", take_over=True)
        ck("with take_over: taken over, with the hand-over", out.startswith("You have taken the build over")
           and "I've paused" in out and "page.tsx" in out and "## Checklist" in out, out[:300])
        s = {x["session"]: x for x in (await cx.get(f"{b}/sessions")).json()["sessions"]}
        ck("the old session is marked replaced", s["build"]["state"] == "replaced", s["build"])
        r = (await cx.get(f"{b}/pickup")).json()
        ck("a replaced session is not offered again", r["previous_session"] is None
           or r["previous_session"]["session"] != "build", r["previous_session"])

        print("== a crashed session ==")
        uid2 = json.loads(await m.create_project("Crash"))["id"]
        b2 = f"/api/projects/{uid2}"
        t3 = (await cx.post(f"{b2}/tickets", json={"title": "Only one"})).json()["id"]
        await ingest(link("gone", -40, "Crash"), said("gone", -39, "working on it"))
        await cx.put(f"{b2}/tickets/{t3}", json={"status": "in_progress"})
        # its last line was 25 minutes ago and it never said it stopped
        await ingest(said("gone", -25, "half way"))
        r = (await cx.get(f"{b2}/pickup")).json()
        ck("quiet for longer than 10 min: not open", r["may_be_open"] is False
           and r["previous_session"]["session"] == "gone"
           and r["previous_session"]["quiet_minutes"] >= 24, r["previous_session"])
        out = await m.pick_up(project="Crash")
        ck("the tool takes it over without asking", out.startswith("You have taken the build over"), out[:200])
        await cx.aclose()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
