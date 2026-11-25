"""Recording a Codex station's session (TR-1595): the crew's recorder and the cell's /ingest/codex.

The recorder (`tares.crew.codex_record`) builds facts from what a visit reads of a station (the conversation
as `thread/read` gives it) and from the guard's record; the cell (`tares.connectors.codex`) turns them into
session lines. The conversations here are shaped as Codex 0.171.0 gave them to
tests/factory/codex_probe/probe_record.py (the item ids are the guard's call ids, a running command is in
no listing, a killed app-server leaves a turn `interrupted` with no item for the command it cut off).

  a. facts from one look, and from the next: starts, ends, what a repeat sends
  b. a start with no end: an ended turn, a killed app-server, a conversation given up
  c. the cell's lines
  d. tokens
  e. the watch's step: what is kept, a post Tares does not take, a station that was retired
  f. through a store: rows, states, usage

Run: .venv/bin/python tests/test_codex_record.py
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tares.connectors import codex as cell  # noqa: E402
from tares.connectors.claude_code import ClaudeCodeConnector  # noqa: E402
from tares.crew import codex_guard  # noqa: E402
from tares.crew import codex_record as rec  # noqa: E402
from tares.crew.client import TaresHTTPError  # noqa: E402
from tares.crew.paths import CrewPaths  # noqa: E402

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"   {detail}"))


NOW = 1795600900.0
THREAD, TURN = "01a2-thread", "01a2-turn-1"
BASE = {"sessionId": THREAD, "factory_station": "crew-builder", "factory_crew": "crew", "factory_machine": "box",
        "station": "crew-builder", "role": "builder", "parent": "crew-orchestrator", "tares_project": "uc_1",
        "cwd": "/w", "model": "gpt-5.6-luna"}


def armed(turn=TURN, at=NOW - 20):
    return {"at": at, "turn": turn, "armed": True}


def allow(call, command, turn=TURN, at=NOW - 10, tool="Bash"):
    line = {"at": at, "turn": turn, "call": call, "tool": tool, "said": "allow", "cwd": "/w"}
    if tool == "Bash":
        line["command"] = command
    return line


def command(call, text, code=0, output="", status="completed", cwd="/w/app"):
    return {"type": "commandExecution", "id": call, "command": f"/bin/zsh -lc 'exec </dev/null\n{text}'", "cwd": cwd,
            "commandActions": [{"type": "unknown", "command": f"exec </dev/null\n{text}"}], "exitCode": code,
            "aggregatedOutput": output, "status": status, "durationMs": 30, "processId": "71", "source": "unifiedExecStartup",
            "startedAtMs": int((NOW - 9) * 1000), "completedAtMs": int((NOW - 8) * 1000)}


def said(iid, text, kind="agentMessage"):
    item = {"type": kind, "id": iid, "startedAtMs": int((NOW - 12) * 1000), "completedAtMs": int((NOW - 11) * 1000)}
    item.update({"content": [{"type": "text", "text": text}]} if kind == "userMessage" else {"text": text, "phase": "commentary"})
    return item


def thread(items, status="inProgress", kind="active", turn=TURN, flags=None, more=()):
    st = {"type": kind}
    if kind == "active":
        st["activeFlags"] = flags or []
    return {"id": THREAD, "status": st, "createdAt": int(NOW - 60), "model": "gpt-5.6-luna", "path": "",
            "turns": [*more, {"id": turn, "status": status, "itemsView": "full", "startedAt": int(NOW - 20), "items": items}]}


def kinds(facts):
    return [(f["kind"], f.get("call") or (f.get("item") or {}).get("id") or f.get("state") or "") for f in facts]


# ---------------------------------------------------------------------------------------------
print("a. facts from one look, and from the next")

record = [armed(), allow("exec-1", "pytest -q"), allow("exec-2", "sleep 40")]
mid = thread([said("u1", "Run the tests.", "userMessage"), said("m1", "I will run them."), command("exec-1", "pytest -q", 0, "14 passed")])
facts1, sent1 = rec.facts(mid, record, rec.new_sent(THREAD), BASE, True, NOW)
ck("the first look says whose session it is, what was said, both starts, the one end, and that it works",
   kinds(facts1) == [("session", ""), ("start", "exec-1"), ("start", "exec-2"), ("item", "u1"), ("item", "m1"),
                     ("item", "exec-1"), ("state", "working")], kinds(facts1))
start2 = next(f for f in facts1 if f["kind"] == "start" and f["call"] == "exec-2")
ck("a start is the guard's line: the command as asked, its folder, the time the guard let it through",
   start2["command"] == "sleep 40" and start2["command_cwd"] == "/w" and start2["turn"] == TURN
   and start2["timestamp"].startswith(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(NOW - 10))), start2)
ck("the running command is a start with no end", list(sent1["starts"]) == ["exec-2"], sent1["starts"])
ck("every fact carries the conversation and the station's stamps",
   all(f["sessionId"] == THREAD and f["factory_station"] == "crew-builder" and f["type"] == "codex" for f in facts1))
again, sent_again = rec.facts(mid, record, sent1, BASE, True, NOW + 1)
ck("the same look again sends nothing", again == [] and sent_again == sent1, again)
ck("the caller's copy of what was sent is not changed by a look", rec.new_sent(THREAD)["items"] == [] and "exec-1" not in rec.new_sent()["items"])

done = thread([*mid["turns"][0]["items"], command("exec-2", "sleep 40", 0, ""), said("m2", "slept")], status="completed", kind="idle")
facts2, sent2 = rec.facts(done, record, sent1, BASE, True, NOW + 50)
ck("the next look sends the end of the command that ran, the answer, and that the station is idle",
   kinds(facts2) == [("item", "exec-2"), ("item", "m2"), ("state", "idle")], kinds(facts2))
ck("no start is sent twice", not [f for f in facts2 if f["kind"] == "start"])
facts3, sent3 = rec.facts(done, record, sent2, BASE, True, NOW + 60)
ck("the look after the turn ended closes it, with nothing more to say", facts3 == [] and sent3["closed"] == [TURN]
   and sent3["items"] == [] and sent3["starts"] == {}, (facts3, sent3))
ck("a closed turn is not gone through again", rec.facts(done, record, sent3, BASE, True, NOW + 70) == ([], sent3))

lone = thread([command("exec-9", "git status", 0, "clean"),
               {"type": "mcpToolCall", "id": "mcp-1", "server": "tares", "tool": "add_check", "arguments": {"ticket": "T1"},
                "status": "completed", "result": {"content": [{"type": "text", "text": "recorded"}]}, "durationMs": 4},
               {"type": "reasoning", "id": "rs1", "summary": [], "content": []}])
factsL, sentL = rec.facts(lone, [armed()], rec.new_sent(THREAD), BASE, True, NOW)
ck("a command the guard's record does not hold gets its start from its item, and an MCP call its tool and arguments",
   kinds(factsL) == [("session", ""), ("start", "exec-9"), ("item", "exec-9"), ("start", "mcp-1"), ("item", "mcp-1"), ("state", "working")]
   and factsL[1]["command"] == "git status" and factsL[3]["tool"] == "mcp__tares__add_check"
   and factsL[3]["arguments"] == {"ticket": "T1"}, kinds(factsL))
ck("an item that is neither a message nor a call is not sent, and not looked at again", "rs1" in sentL["items"])
running = thread([{**command("exec-5", "make", None, "", "inProgress")}])
ck("an item still in progress waits for its end", [k for k in kinds(rec.facts(running, [armed()], rec.new_sent(THREAD), BASE, True, NOW)[0])
                                                     if k[0] == "item"] == [])
waiting = thread([], flags=["waitingOnApproval"])
ck("a station that waits for an approval is waiting, and the reason is Codex's own word",
   [(f["state"], f["reason"]) for f in rec.facts(waiting, [], rec.new_sent(THREAD), BASE, True, NOW)[0] if f["kind"] == "state"]
   == [("waiting", "waitingOnApproval")])
long = thread([command("exec-6", "cat big", 0, "x" * 50000 + "TAIL")])
big = next(f for f in rec.facts(long, [], rec.new_sent(THREAD), BASE, True, NOW)[0] if f["kind"] == "item")
ck("a long output keeps its end", len(big["item"]["aggregatedOutput"]) == rec.OUTPUT_KEPT and big["item"]["aggregatedOutput"].endswith("TAIL"))

# ---------------------------------------------------------------------------------------------
print("b. a start with no end")

cut = thread([said("u1", "Run it.", "userMessage")], status="interrupted", kind="idle")
f1, s1 = rec.facts(cut, record, sent1, BASE, True, NOW + 30)
ck("an ended turn with a start and no item says nothing at the look that first sees it ended",
   [k for k in kinds(f1) if k[0] == "unknown"] == [] and s1["ended"] == [TURN], kinds(f1))
f2, s2 = rec.facts(cut, record, s1, BASE, True, NOW + 40)
ck("the look after says the command's outcome is unknown, once",
   kinds(f2) == [("unknown", "exec-2")] and "interrupted" in f2[0]["why"] and s2["starts"] == {} and s2["closed"] == [TURN], f2)
ck("and never again", rec.facts(cut, record, s2, BASE, True, NOW + 50)[0] == [])
late = thread([*cut["turns"][0]["items"], command("exec-2", "sleep 40", 0, "")], status="completed", kind="idle")
f_late, _ = rec.facts(late, record, s1, BASE, True, NOW + 40)
ck("an item that is listed by the second look is the command's end, not an unknown outcome",
   kinds(f_late) == [("item", "exec-2")], kinds(f_late))

dead, s_dead = rec.facts(None, [*record, allow("exec-3", "make e2e", at=NOW - 1)], sent1, BASE, False, NOW + 5)
ck("a station that is not running has ended; a new start in a turn that was seen is still sent",
   kinds(dead) == [("start", "exec-3"), ("state", "ended")] and dead[1]["reason"] == "the station is not running", kinds(dead))
unseen = [*record, allow("exec-4", "make", turn="01a2-turn-2", at=NOW + 3)]
held, s_held = rec.facts(None, unseen, sent1, BASE, False, NOW + 5)
ck("a start in a turn no look has listed waits while the station cannot be read",
   [k for k in kinds(held) if k[0] == "start"] == [] and "exec-4" not in s_held["starts"], kinds(held))
back = thread([], status="interrupted", kind="idle")   # read again after a new start: the second turn is in no listing
f_back, s_back = rec.facts(back, unseen, s_held, BASE, True, NOW + 90)
ck("once the conversation is read and does not hold that turn, the command is shown as started with an unknown outcome",
   [k for k in kinds(f_back) if k[1] == "exec-4"] == [("start", "exec-4"), ("unknown", "exec-4")], kinds(f_back))
ck("and is not said again", [k for k in kinds(rec.facts(back, unseen, s_back, BASE, True, NOW + 99)[0]) if k[1] == "exec-4"] == [])
fresh = thread([], kind="active")
fresh["turns"][0]["startedAt"] = int(NOW + 2)
f_fresh, _ = rec.facts(fresh, unseen, sent1, BASE, True, NOW + 4)
ck("while a turn runs, a start as new as the newest listed turn waits for its own turn to be listed",
   [k for k in kinds(f_fresh) if k[1] == "exec-4"] == [], kinds(f_fresh))

closing = rec.closing(sent1, BASE, "the conversation was given up before its end was recorded", NOW)
ck("a conversation that is given up: every start with no end has an unknown outcome, and the session has ended",
   kinds(closing) == [("unknown", "exec-2"), ("state", "ended")], kinds(closing))
newer = {**thread([], kind="idle", status="completed", turn="01a2-turn-9"), "id": "01a2-thread-2", "createdAt": int(NOW + 100)}
after = {**rec.new_sent("01a2-thread-2"), "before": THREAD}
f_new, s_new = rec.facts(newer, [*record, allow("exec-7", "git push", at=NOW + 20)], after, {**BASE, "sessionId": "01a2-thread-2"}, True, NOW + 120)
mine = [f for f in f_new if f.get("call") == "exec-7"]
ck("a start older than the new conversation that no look had sent is shown in the one before, with an unknown outcome",
   [(f["kind"], f["sessionId"]) for f in mine] == [("start", THREAD), ("unknown", THREAD)], mine)
ck("the starts the old conversation had already sent are not sent into it again",
   not [f for f in f_new if f.get("call") in ("exec-1", "exec-2") and f["kind"] == "start" and f["sessionId"] == "01a2-thread-2"])

# ---------------------------------------------------------------------------------------------
print("c. the cell's lines")

lines = cell.session_lines(facts1 + facts2)
by_uuid = {x["uuid"]: x for x in lines}
ck("every line has an id made of its conversation and its item, so a line sent twice is the same line",
   len(by_uuid) == len(lines) and all(u.startswith(f"codex:{THREAD}:") for u in by_uuid), sorted(by_uuid))
station = by_uuid[f"codex:{THREAD}:station"]
ck("the session is tied to its station, crew, machine and project",
   (station["type"], station["station"], station["role"], station["parent"], station["crew"], station["machine"],
    station["tares_project"]) == ("session_station", "crew-builder", "builder", "crew-orchestrator", "crew", "box", "uc_1")
   and by_uuid[f"codex:{THREAD}:project"]["type"] == "session_project", station)
use = by_uuid[f"codex:{THREAD}:exec-2:start"]
block = use["message"]["content"][0]
ck("a start is a Bash call keyed by conversation and call, with the outcome started and its folder",
   block == {"type": "tool_use", "id": f"codex:{THREAD}:exec-2", "name": "Bash", "input": {"command": "sleep 40"}}
   and use["codex"]["outcome"] == "started" and use["codex"]["cwd"] == "/w" and use["type"] == "assistant", use)
end = by_uuid[f"codex:{THREAD}:exec-1:end"]
ck("an end is that call's result, with the real exit code, the folder it ran in and the command as it was asked",
   end["message"]["content"][0] == {"type": "tool_result", "tool_use_id": f"codex:{THREAD}:exec-1", "content": "14 passed",
                                    "is_error": False}
   and end["codex"]["exit_code"] == 0 and end["codex"]["cwd"] == "/w/app" and end["codex"]["command"] == "pytest -q"
   and end["codex"]["outcome"] == "ended", end)
prompt = by_uuid[f"codex:{THREAD}:u1"]
ck("a prompt is a user line that says the crew sent it, so it is never counted as a person typing",
   prompt["type"] == "user" and prompt["origin"] == {"kind": "crew"} and prompt["message"]["content"] == "Run the tests.", prompt)
ck("what the station said is an assistant line with its text and model",
   by_uuid[f"codex:{THREAD}:m1"]["message"]["content"] == [{"type": "text", "text": "I will run them."}]
   and by_uuid[f"codex:{THREAD}:m1"]["message"]["model"] == "gpt-5.6-luna")
states = [x for x in lines if x["type"] == "session_state"]
ck("the states are session_state lines", [x["state"] for x in states] == ["working", "idle"], states)
failed = cell.session_lines([{**BASE, "type": "codex", "kind": "item", "timestamp": "t", "turn": TURN,
                              "item": command("exec-8", "sh -c 'exit 7'", 7, "boom", "failed")}])[0]
ck("a command that exited 7 keeps 7, and its result is an error",
   failed["codex"]["exit_code"] == 7 and failed["message"]["content"][0]["is_error"] is True
   and failed["codex"]["command"] == "sh -c 'exit 7'", failed)
declined = cell.session_lines([{**BASE, "type": "codex", "kind": "item", "timestamp": "t", "turn": TURN,
                                "item": command("exec-d", "rm -rf x", None, "", "declined")}])[0]
ck("a command Codex gave no exit code for keeps none (never 0) and Codex's own word for how it ended",
   declined["codex"]["exit_code"] is None and declined["codex"]["outcome"] == "declined"
   and declined["message"]["content"][0]["is_error"] is True, declined)
unknown = cell.session_lines(f2)[0]
ck("an unknown outcome is a result that says so, with no exit code",
   unknown["codex"] == {"turn": TURN, "exit_code": None, "outcome": "unknown"}
   and unknown["message"]["content"][0]["content"].startswith("outcome unknown: "), unknown)
mcp = cell.session_lines(factsL)
ck("an MCP call is a call of mcp__server__tool with its arguments, and its result",
   [b for x in mcp for b in (x.get("message") or {}).get("content", []) if isinstance(b, dict) and b.get("name") == "mcp__tares__add_check"]
   [0]["input"] == {"ticket": "T1"}
   and any(b.get("content") == "recorded" for x in mcp for b in (x.get("message") or {}).get("content", []) if isinstance(b, dict)), mcp)
ck("the guard's added line and the login shell are taken off a command",
   cell.asked("/bin/zsh -lc 'exec </dev/null\necho it'\\''s'") == "echo it's" and cell.asked("exec </dev/null\nls") == "ls"
   and cell.asked("ls -la") == "ls -la" and cell.asked(None) == ""
   and cell.CLOSE_STDIN == codex_guard.CLOSE_STDIN)
ck("what is not a fact of a known kind makes no line",
   cell.session_lines([{"type": "assistant", "sessionId": "x"}, {"type": "codex", "kind": "start"}, "text", None,
                       {"type": "codex", "kind": "nope", "sessionId": "x"}, {"type": "codex", "kind": "state", "sessionId": "x", "state": "odd"}]) == [])

# ---------------------------------------------------------------------------------------------
print("d. tokens")

tmp = Path(tempfile.mkdtemp(prefix="codex_record_"))
rollout = tmp / "rollout.jsonl"


def count(n_in, cached, out, **more):
    return {"timestamp": "2026-11-25T10:00:00.000Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {}, "last_token_usage": {"input_tokens": n_in, "cached_input_tokens": cached,
                                                      "cache_write_input_tokens": 0, "output_tokens": out,
                                                      "reasoning_output_tokens": 3, "total_tokens": n_in + out, **more}}}}


rollout.write_text("\n".join(json.dumps(x) for x in [
    {"type": "session_meta", "payload": {"id": THREAD}},
    count(1200, 1000, 40),
    {"timestamp": "t", "type": "event_msg", "payload": {"type": "token_count", "info": None}},      # limits only
    {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": "soon"}}},  # another shape
    count(900, 800, 6)]) + "\nnot json\n")
usage, n = rec.tokens(str(rollout), 0, BASE)
ck("one fact per reply Codex counted, numbered in the file's order; a line of another shape is passed over",
   n == 2 and [f["n"] for f in usage] == [1, 2] and usage[0]["usage"]["input_tokens"] == 1200
   and usage[0]["timestamp"] == "2026-11-25T10:00:00.000Z", usage)
ck("only the replies after the ones already sent", [f["n"] for f in rec.tokens(str(rollout), 1, BASE)[0]] == [2]
   and rec.tokens(str(rollout), 2, BASE) == ([], 2))
ck("a file that is not there gives nothing and keeps the count", rec.tokens(str(tmp / "none"), 2, BASE) == ([], 2))
line = cell.session_lines(usage)[0]
ck("the cell keeps the cached tokens apart from the input, as it does for Claude Code",
   line["type"] == "session_usage" and line["message_id"] == "codex:usage:1" and line["model"] == "gpt-5.6-luna"
   and line["usage"] == {"input_tokens": 200, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0,
                         "output_tokens": 40}, line)
part = cell.session_lines([{**usage[0], "usage": {"output_tokens": 5}}])[0]
ck("a count Codex did not give is left out, not sent as 0", part["usage"] == {"output_tokens": 5}, part)
ck("a usage fact with no count makes no line", cell.session_lines([{**usage[0], "usage": {}}]) == [])

# ---------------------------------------------------------------------------------------------
print("e. the watch's step")

home = tmp / "crew"
(home / "stations").mkdir(parents=True)
paths = CrewPaths.at(str(home))
S = "crew-builder"
(home / "stations.json").write_text(json.dumps({
    S: {"runtime": "codex", "role": "builder", "parent": "crew-orchestrator", "cwd": "/w", "model": "", "sessionId": THREAD},
    "crew-claude": {"role": "reviewer", "sessionId": "abc"}}))
(home / "stations" / f"{S}.settings.json").write_text(json.dumps({"env": {"FACTORY_CREW": "crew", "TARES_PROJECT": "uc_1"}}))
Path(codex_guard.record_file(str(home), S)).write_text("".join(json.dumps(x) + "\n" for x in record))


class Tares:
    def __init__(self):
        self.posts, self.fail = [], False
        self.paths, self.env = paths, {"FACTORY_MACHINE": "box"}

    def tares_http(self, method, path, body=None, headers=None):
        if self.fail:
            raise TaresHTTPError("curl: (22) The requested URL returned error: 503", 503, "")
        self.posts.append((method, path, body))
        return "{}"


LOOK = {"found": mid, "alive": True}
rec.read = lambda paths, station, thread: LOOK["found"]
rec.cs = SimpleNamespace(is_alive=lambda paths, station: LOOK["alive"])
tares = Tares()
tares.fail = True
got = rec.step(tares)
ck("a post Tares does not take is an error for that station, and nothing is kept as sent",
   [r["station"] for r in got] == [S] and "503" in got[0]["error"] and not (home / "stations" / f"{S}.codex.sent").exists(), got)
tares.fail = False
got = rec.step(tares)
posted = [f for _, _, body in tares.posts for f in body]
ck("the next round posts it to /ingest/codex; only the Codex station is looked at",
   got == [{"station": S, "sent": 7, "error": ""}] and tares.posts[0][:2] == ("POST", "/ingest/codex")
   and kinds(posted) == kinds(facts1), (got, kinds(posted)))
ck("the stamps come from the station's record and settings, the model from the conversation",
   posted[0]["factory_crew"] == "crew" and posted[0]["tares_project"] == "uc_1" and posted[0]["factory_machine"] == "box"
   and posted[0]["role"] == "builder" and posted[0]["model"] == "gpt-5.6-luna", posted[0])
kept = home / "stations" / f"{S}.codex.sent"
ck("what was sent is kept in stations/<station>.codex.sent, the owner's alone",
   json.loads(kept.read_text())["starts"].keys() == {"exec-2"} and oct(kept.stat().st_mode & 0o777) == "0o600")
tares.posts.clear()
ck("a round with nothing new posts nothing", rec.step(tares) == [{"station": S, "sent": 0, "error": ""}] and tares.posts == [])
LOOK.update(found=None, alive=False)
rec.step(tares)
ck("a station that is not running is recorded as ended", kinds([f for _, _, b in tares.posts for f in b]) == [("state", "ended")])

tares.posts.clear()
rows = json.loads((home / "stations.json").read_text())
rows[S]["sessionId"] = "01a2-thread-2"
(home / "stations.json").write_text(json.dumps(rows))
LOOK.update(found={**thread([], kind="idle", status="completed", turn="01a2-turn-9"), "id": "01a2-thread-2",
                   "createdAt": int(time.time()) + 5}, alive=True)
got = rec.step(tares)
first, second = tares.posts[0][2], tares.posts[1][2]
ck("a new conversation: the old one's open start gets an unknown outcome under the old session, then the new one begins",
   kinds(first) == [("unknown", "exec-2")] and first[0]["sessionId"] == THREAD
   and second[0]["kind"] == "session" and second[0]["sessionId"] == "01a2-thread-2"
   and json.loads(kept.read_text())["before"] == THREAD, (kinds(first), kinds(second)))

tares.posts.clear()
(home / "stations.json").write_text(json.dumps({"crew-claude": {"role": "reviewer"}}))
got = rec.step(tares)
ck("a station that was retired: its session has ended, and its file is removed",
   [r["station"] for r in got] == [S] and kinds(tares.posts[0][2]) == [("state", "ended")] and not kept.exists(), (got, tares.posts))
ck("a crew with no Codex station posts nothing", rec.step(tares) == [] and len(tares.posts) == 1)
(home / "stations.json").write_text("not json")
ck("a crew file that cannot be read gives nothing and does not raise", rec.step(tares) == [])

# ---------------------------------------------------------------------------------------------
print("f. through a store")

from tares.store import Store  # noqa: E402

store = Store(str(tmp / "t.duckdb"))
conn = ClaudeCodeConnector(SimpleNamespace(name="claude_code", type="agent_session", config={}), store)
everything = facts1 + facts2 + usage
envelopes = conn.map_payload(cell.session_lines(everything))
store.append(envelopes)
rows = {c["command"]: c for c in store.recorded_commands("claude_code", [THREAD])}
ck("the commands are rows of session_commands with their exit code, folder and outcome",
   rows["pytest -q"]["outcome"] == "ended" and rows["pytest -q"]["exit_code"] == 0 and rows["pytest -q"]["output"] == "14 passed"
   and rows["pytest -q"]["cwd"] == "/w/app" and rows["sleep 40"]["outcome"] == "ended", rows)
store.append(conn.map_payload(cell.session_lines(everything)))
ck("the same facts posted again add no row", len(store.recorded_commands("claude_code", [THREAD])) == 2)
store.append(conn.map_payload(cell.session_lines([f for f in facts1 if f["kind"] == "start"])))
ck("a start that arrives after the end does not take the end back",
   store.recorded_commands("claude_code", [THREAD])[0]["outcome"] == "ended")
with store.sessions_db.begin() as c:
    from sqlalchemy import select  # noqa: E402

    from tares.session_store.tables import session_usage  # noqa: E402
    used = c.execute(select(session_usage.c.message, session_usage.c.input_tokens, session_usage.c.cache_read_tokens,
                            session_usage.c.output_tokens, session_usage.c.cost_usd, session_usage.c.model)
                     .where(session_usage.c.session == THREAD).order_by(session_usage.c.message)).fetchall()
ck("the tokens are counted once per reply, and the cost of a model Tares has no price for is unknown, not 0",
   [tuple(r) for r in used] == [("codex:usage:1", 200, 1000, 40, None, "gpt-5.6-luna"),
                               ("codex:usage:2", 100, 800, 6, None, "gpt-5.6-luna")], used)
store.record_reported_usage(THREAD, "codex:usage:3", None, "gpt-5.6-luna", None, {"output_tokens": 9})
with store.sessions_db.begin() as c:
    row = c.execute(select(session_usage.c.input_tokens, session_usage.c.output_tokens, session_usage.c.cache_read_tokens)
                    .where(session_usage.c.message == "codex:usage:3")).fetchone()
ck("a count that was not reported is stored as unknown, never as 0", tuple(row) == (None, 9, None), row)
ck("usage lines are not lines of the timeline", not [e for e in envelopes if (e.payload or {}).get("type") == "session_usage"])
ck("a prompt from the crew is not a person's turn", store.person_turns("claude_code", [THREAD], datetime(2020, 1, 1, tzinfo=timezone.utc)) == [])
ck("the session's state is the last one sent", (store.session_state(THREAD) or {}).get("state") == "idle", store.session_state(THREAD))
mine = [r for r in store.station_sessions("claude_code") if r["session"] == THREAD]
ck("the session is listed as its station's, with its role",
   len(mine) == 1 and mine[0]["station"] == "crew-builder" and mine[0]["role"] == "builder", mine)
from tares import crew_conversation  # noqa: E402

shown, _ = crew_conversation.build([{"event_type": e.event_type, "event_time": e.event_time, "payload": e.payload,
                                     "text": e.text} for e in sorted(envelopes, key=lambda e: e.event_time)])
tools = {i["id"]: i for i in shown if i.get("kind") == "tool"}
ck("the Crew page's conversation shows each command as one call with its output, and what the station said",
   tools[f"codex:{THREAD}:exec-1"]["output"] == "14 passed" and tools[f"codex:{THREAD}:exec-1"]["failed"] is False
   and tools[f"codex:{THREAD}:exec-1"]["name"] == "Bash" and len(tools) == 2
   and [i["text"] for i in shown if i.get("kind") == "said"] == ["I will run them.", "slept"], shown)
store.con.close()

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
