"""A session's recording lands on its project (TR-405).

The plugin side: which tool calls tie a session to a project (create_project by name, a Tares tool
naming `project`; not another server's tool), the stamp on every line from then on, the synthetic
session_project line, and a new project switching it. The Tares side: a session_project line ties
the session to the project, the claude_code source joins the project, and the project lists the
session with every line, the brainstorm from before the project existed included.

Run: .venv/bin/python tests/test_session_project.py
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
sys.path.insert(0, os.path.join(ROOT, "claude-plugin", "scripts"))

_TMP = tempfile.mkdtemp(prefix="tares-session-project-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-sp"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

import ship

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


SID = "sess-1234"


# the session's times: ten minutes ago onwards, so a waiting session is not yet quiet (TR-473)
_BASE = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)


def at(minutes, seconds):
    return (_BASE + timedelta(minutes=minutes, seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def line(i, text=None, tool=None, inp=None):
    o = {"type": "assistant" if tool else "user", "sessionId": SID, "cwd": "/w/invoices",
         "timestamp": at(0, i), "uuid": f"u{i}"}
    if tool:
        o["message"] = {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{i}", "name": tool, "input": inp or {}}]}
    else:
        o["message"] = {"role": "user", "content": text or f"line {i}"}
    return o


def plugin():
    print("== the plugin ==")
    ck("create_project names the project",
       ship.project_call(line(1, tool="mcp__tares__create_project", inp={"name": "Invoices"}))
       == "Invoices")
    ck("from the plugin's server too",
       ship.project_call(line(1, tool="mcp__plugin_tares_tares__write_doc",
                              inp={"project": "Invoices", "kind": "spec"})) == "Invoices")
    ck("from a copy of the plugin under another name",
       ship.project_call(line(1, tool="mcp__plugin_tares-dev_tares__create_project",
                              inp={"name": "Invoices"})) == "Invoices")
    ck("a Tares tool without a project says nothing",
       ship.project_call(line(1, tool="mcp__tares__list_projects")) is None)
    ck("another server's create_project is not ours",
       ship.project_call(line(1, tool="mcp__linear__create_project", inp={"name": "X"})) is None)
    ck("a plain line says nothing", ship.project_call(line(1, "hello")) is None)
    ck("the build sentence names the project (TR-474)", ship.project_call(line(
        1, 'Work on Tares project "shared-mcp-endpoint": read its starting prompt and begin.'))
       == "shared-mcp-endpoint")
    ck("so does the pick-up sentence", ship.project_call(line(
        1, 'Pick up Tares project "Invoices" where the last session stopped.')) == "Invoices")
    blocks = line(1)
    blocks["message"]["content"] = [{"type": "text", "text": 'Work on Tares project "Invoices": go'}]
    ck("in a message made of text blocks too", ship.project_call(blocks) == "Invoices")
    ck("not when the sentence is only quoted inside other text", ship.project_call(line(
        1, 'what does Work on Tares project "Invoices" mean?')) is None)
    said = line(1, tool="x")
    said["message"] = {"role": "assistant",
                       "content": [{"type": "text", "text": 'Work on Tares project "Invoices"'}]}
    ck("nor when Claude says it", ship.project_call(said) is None)
    hook0 = {"session_id": SID, "cwd": "/w/invoices"}
    o, pj, ch = ship.stamp_project([line(1, 'Work on Tares project "Invoices": begin'),
                                    line(2, "reading the start doc")], "", hook0)
    ck("a build session is tied to the project from that message",
       ch and pj == "Invoices" and o[1]["type"] == "session_project", o)
    hook = {"session_id": SID, "cwd": "/w/invoices"}
    objs = [line(1, "let's build an invoice parser"), line(2, "what about emails?"),
            line(3, tool="mcp__tares__create_project", inp={"name": "Invoices"}),
            line(4, "now the spec")]
    out, project, changed = ship.stamp_project(objs, "", hook)
    ck("changed to the new project", changed and project == "Invoices")
    ck("lines before it are not stamped (Tares ties them by session)",
       "tares_project" not in out[0] and "tares_project" not in out[1])
    ck("the call and every line after are stamped",
       out[2]["tares_project"] == "Invoices" and out[4]["tares_project"] == "Invoices")
    sp = out[3]
    ck("a session_project line right after the call",
       sp["type"] == "session_project" and sp["tares_project"] == "Invoices"
       and sp["sessionId"] == SID, sp)
    out2, project2, changed2 = ship.stamp_project([line(5, "more")], "Invoices", hook)
    ck("the next batch keeps the project without a new line",
       not changed2 and len(out2) == 1 and out2[0]["tares_project"] == "Invoices")
    out3, project3, changed3 = ship.stamp_project(
        [line(6, tool="mcp__tares__get_doc", inp={"project": "Billing", "id": "d"})], "Invoices",
        hook)
    ck("naming another project switches it", changed3 and project3 == "Billing"
       and out3[1]["type"] == "session_project")
    book = [{"type": t, "sessionId": SID} for t in ("attachment", "queue-operation", "mode",
                                                     "permission-mode", "ai-title", "last-prompt")]
    kept, _, _ = ship.process_lines([json.dumps(o) for o in book + [line(9, "a real line")]], "",
                                    hook)
    ck("Claude Code's bookkeeping lines are not shipped", [o["type"] for o in kept] == ["user"],
       kept)
    print("== the plugin: session state ==")
    ck("hooks map to states",
       ship.state_for("PostToolUse", {}) == ("working", "")
       and ship.state_for("SessionEnd", {"reason": "exit"}) == ("ended", "exit")
       and ship.state_for("SessionStart", {}) == (None, None))
    print("== the plugin: a turn that ended is idle, only a real need is waiting (TR-693) ==")
    ck("a turn that ended is idle, not waiting for the person",
       ship.state_for("Stop", {}) == ("idle", ship.IDLE_REASON))
    ck("Claude Code's idle notice (idle_prompt) is idle too, with the same reason",
       ship.state_for("Notification", {"notification_type": "idle_prompt",
                                       "message": "Claude is waiting for your input"})
       == ("idle", ship.IDLE_REASON))
    ck("a permission prompt waits for the person",
       ship.state_for("Notification", {"notification_type": "permission_prompt",
                                       "message": "Claude needs your permission to use Bash"})
       == ("waiting", "Claude needs your permission to use Bash"))
    ck("an elicitation dialog waits for the person",
       ship.state_for("Notification", {"notification_type": "elicitation_dialog",
                                       "message": "Claude needs your input"})[0] == "waiting")
    ck("a question to the person waits for the person",
       ship.state_for("PreToolUse", {"tool_name": "AskUserQuestion"})[0] == "waiting"
       and ship.state_for("PreToolUse", {"tool_name": "Bash"}) == (None, None))
    ck("a permission request waits for the person at once, naming the tool",
       ship.state_for("PermissionRequest", {"tool_name": "Bash"})
       == ("waiting", "Claude Code needs your permission to use Bash"))
    ck("a URL elicitation dialog waits for the person",
       ship.state_for("Notification", {"notification_type": "elicitation_url_dialog",
                                       "message": "Open this link"})[0] == "waiting")
    hooks = json.load(open(os.path.join(ROOT, "claude-plugin", "hooks", "hooks.json")))["hooks"]

    def ships(entry):
        return any("scripts/ship.py" in " ".join(h.get("args") or []) for h in entry["hooks"])
    ck("the plugin registers PermissionRequest with the shipper",
       any(ships(e) for e in hooks.get("PermissionRequest", [])), hooks.get("PermissionRequest"))
    ck("the plugin registers the AskUserQuestion PreToolUse hook with the shipper",
       any(e.get("matcher") == "AskUserQuestion" and ships(e) for e in hooks.get("PreToolUse", [])),
       hooks.get("PreToolUse"))
    ck("a notification of another type says nothing new",
       ship.state_for("Notification", {"notification_type": "auth_success",
                                       "message": "Authenticated"}) == (None, None))
    ck("without a type (older Claude Code) the message tells permission from idle",
       ship.state_for("Notification", {"message": "Claude needs your permission to use Bash"})[0]
       == "waiting"
       and ship.state_for("Notification", {"message": "Claude is waiting for your input"})[0]
       == "idle"
       and ship.state_for("Notification", {"message": "something else"}) == (None, None))
    sent = []
    real = ship.ship_lines
    ship.ship_lines = lambda cfg, objs: sent.extend(objs) or True
    try:
        cfg = {"base": "http://x", "headers": {}, "data_dir": tempfile.mkdtemp()}
        ship.write_project(cfg["data_dir"], SID, "Invoices")
        h = {"session_id": SID, "cwd": "/w/invoices"}
        idle_notice = {**h, "notification_type": "idle_prompt",
                       "message": "Claude is waiting for your input"}
        ask = {**h, "notification_type": "permission_prompt",
               "message": "Claude needs your permission to use Bash"}
        for ev, hook in (("PostToolUse", h), ("PostToolUse", h), ("PostToolUse", h), ("Stop", h),
                         ("Notification", idle_notice), ("Stop", h), ("UserPromptSubmit", h),
                         ("Notification", ask), ("PostToolUse", h), ("SessionEnd", h)):
            ship.report_state(cfg, hook, ev, "")
        ck("one line per change, not per tool call; the idle notice after Stop adds none",
           [o["state"] for o in sent] == ["working", "idle", "working", "waiting", "working",
                                           "ended"], sent)
        ck("the line carries the project", all(o.get("tares_project") == "Invoices" for o in sent))
    finally:
        ship.ship_lines = real

    print("== the plugin: the end is reported first (TR-473) ==")
    import io
    posts = []

    def fake_ship(cfg, objs):   # the transcript's post fails; the state line goes through
        posts.append([o.get("type") for o in objs])
        return all(o.get("type") == "session_state" for o in objs)

    real = (ship.ship_lines, ship.config, sys.stdin)
    try:
        dd = tempfile.mkdtemp()
        cfg = {"base": "http://x", "headers": {}, "data_dir": dd}
        open(os.path.join(dd, "ensured-" + __import__("hashlib").sha1(b"http://x").hexdigest()[:12]),
             "w").write("1")
        tr = os.path.join(dd, "t.jsonl")
        open(tr, "w").write(json.dumps(line(1, "bye")) + "\n")
        ship.ship_lines, ship.config = fake_ship, (lambda: cfg)
        ship.report_state(cfg, {"session_id": SID, "cwd": "/w"}, "Stop", "")
        posts.clear()
        sys.stdin = io.StringIO(json.dumps({"hook_event_name": "SessionEnd", "session_id": SID,
                                            "cwd": "/w", "reason": "exit",
                                            "transcript_path": tr}))
        ship.main()
        states = [p for p in posts if p == ["session_state"]]
        ck("SessionEnd sends the end before anything else", posts and posts[0] == ["session_state"],
           posts)
        ck("even when the transcript's post fails, and only once", len(states) == 1, posts)
    finally:
        ship.ship_lines, ship.config, sys.stdin = real

    d = tempfile.mkdtemp()
    ship.write_project(d, SID, "Invoices")
    ck("the marker round-trips", ship.read_project(d, SID) == "Invoices"
       and ship.read_project(d, "other") == "")
    return out


async def tares(shipped):
    print("== Tares ==")
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": f"Bearer {TOKEN}"})
        r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                "poll": "10s", "config": {"push": True}})
        ck("claude_code source", r.status_code in (200, 201), r.text)
        uid = (await cx.post("/api/projects", json={"template": "custom", "name": "Invoices",
                                                    "objects": []})).json()["id"]
        nd = "\n".join(json.dumps(o) for o in shipped) + "\n"
        r = await cx.post("/ingest/claude_code", content=nd,
                          headers={"Content-Type": "application/x-ndjson"})
        ck("ingest the session", r.status_code == 202, r.text)
        r = await cx.get(f"/api/projects/{uid}/sessions")
        ss = r.json()["sessions"]
        ck("the project lists the session with its repo", len(ss) == 1 and ss[0]["session"] == SID
           and ss[0]["repo"] == "invoices", ss)
        ck("every line counts, the brainstorm before the project included",
           ss[0]["lines"] == len(shipped), ss)
        r = await cx.get(f"/api/projects/{uid}/sessions/{SID}")
        texts = [x["text"] for x in r.json()["lines"]]
        ck("read the session top to bottom", texts[0] == "let's build an invoice parser"
           and "session works in project Invoices" in texts, texts)
        ck("the claude_code source is in the project", "claude_code" in store.project_sources(uid))
        print("== Tares: session state ==")
        ck("no state before the plugin reports one", ss[0]["state"] is None, ss[0])
        said = {"type": "assistant", "sessionId": SID, "timestamp": at(1, 0),
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "I've paused: the safety check stopped answering."}]}}
        waiting = {"type": "session_state", "sessionId": SID, "state": "waiting",
                   "reason": "Claude needs your permission to use Bash",
                   "timestamp": at(1, 5)}
        stale = {**waiting, "state": "working", "reason": "", "timestamp": at(0, 59)}
        await cx.post("/ingest/claude_code", content="\n".join(json.dumps(o) for o in
                      (said, waiting, stale)) + "\n", headers={"Content-Type": "application/x-ndjson"})
        s = (await cx.get(f"/api/projects/{uid}/sessions")).json()
        one = s["sessions"][0]
        ck("a waiting session, with the reason, the time and what it said last",
           one["state"] == "waiting" and one["state_reason"].startswith("Claude needs")
           and one["last_said"] == "I've paused: the safety check stopped answering."
           and s["now"], one)
        ck("an older line does not win", one["state"] == "waiting")
        idle = {**waiting, "state": "idle", "reason": "the turn ended", "timestamp": at(3, 0)}
        await cx.post("/ingest/claude_code", content=json.dumps(idle) + "\n",
                      headers={"Content-Type": "application/x-ndjson"})
        one = (await cx.get(f"/api/projects/{uid}/sessions")).json()["sessions"][0]
        ck("then idle: a turn that ended is kept as idle, with no last_said (nobody is asked)",
           one["state"] == "idle" and one["state_reason"] == "the turn ended"
           and "last_said" not in one, one)
        ended = {"type": "session_state", "sessionId": SID, "state": "ended", "reason": "exit",
                 "timestamp": at(5, 0)}
        await cx.post("/ingest/claude_code", content=json.dumps(ended) + "\n",
                      headers={"Content-Type": "application/x-ndjson"})
        one = (await cx.get(f"/api/projects/{uid}/sessions")).json()["sessions"][0]
        ck("then ended, and no last_said for a session that is not waiting",
           one["state"] == "ended" and "last_said" not in one, one)
        bogus = {"type": "session_state", "sessionId": SID, "state": "asleep",
                 "timestamp": at(6, 0)}
        await cx.post("/ingest/claude_code", content=json.dumps(bogus) + "\n",
                      headers={"Content-Type": "application/x-ndjson"})
        one = (await cx.get(f"/api/projects/{uid}/sessions")).json()["sessions"][0]
        ck("an unknown state is ignored", one["state"] == "ended", one)
        print("== Tares: a session whose end never arrived does not wait forever (TR-473) ==")
        now = datetime.now(timezone.utc)
        iso = lambda d: (now - d).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for sid, state, ago in (("q-wait-old", "waiting", timedelta(minutes=100)),
                                ("q-wait-new", "waiting", timedelta(minutes=30)),
                                ("q-idle-old", "idle", timedelta(minutes=100)),
                                ("q-idle-new", "idle", timedelta(minutes=30)),
                                ("q-work-old", "working", timedelta(minutes=20))):
            await cx.post("/ingest/claude_code", content="\n".join(json.dumps(o) for o in (
                {"type": "session_project", "sessionId": sid, "tares_project": "Invoices",
                 "timestamp": iso(ago)},
                {"type": "session_state", "sessionId": sid, "state": state, "reason": "",
                 "timestamp": iso(ago)})) + "\n", headers={"Content-Type": "application/x-ndjson"})
        got = {x["session"]: x["state"]
               for x in (await cx.get(f"/api/projects/{uid}/sessions")).json()["sessions"]}
        ck("waiting, silent past the 90-minute window: quiet", got.get("q-wait-old") == "quiet", got)
        ck("waiting for half an hour: still waiting", got.get("q-wait-new") == "waiting", got)
        ck("idle, silent past the 90-minute window: quiet", got.get("q-idle-old") == "quiet", got)
        ck("idle for half an hour: still idle, not a stopped build", got.get("q-idle-new") == "idle",
           got)
        ck("working, silent for 20 minutes: quiet", got.get("q-work-old") == "quiet", got)
        ck("an ended session stays ended", got.get(SID) == "ended", got)
        r = await cx.get(f"/api/projects/{uid}/sessions/other")
        ck("a session that did not work here -> 404", r.status_code == 404, r.text)
        r = await cx.post("/ingest/claude_code", content=json.dumps(
            {"type": "session_project", "sessionId": "s2", "tares_project": "Nope",
             "timestamp": at(1, 0)}) + "\n",
            headers={"Content-Type": "application/x-ndjson"})
        ck("an unknown project is ignored, the line still lands", r.status_code == 202
           and store.session_projects("s2") == [], r.text)
        await cx.delete(f"/api/projects/{uid}")
        ck("deleting the project forgets its sessions", store.session_projects(SID) == [])
        await cx.aclose()


if __name__ == "__main__":
    shipped = plugin()
    asyncio.run(tares(shipped))
    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)
