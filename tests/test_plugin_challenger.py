"""The plugin side of the challenger workflow (TR-198, TR-199, TR-200): the shipper marks a
session when it sees the set_session_flow call, stamps flow on every line and writes the
session_flow / session_end lines; the challenger hooks review plans and commits with a fake Codex
(and with Grok against a fake xAI endpoint), block and loop like codex-review, ship challenge events to a fake Tares, and stay inert for an
unmarked session; SessionStart hands accepted memory to Claude.
Run: .venv/bin/python tests/test_plugin_challenger.py   (no Codex, no xAI, no daemon, no network)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import clean_env, scrub_os_environ

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHIP = os.path.join(ROOT, "claude-plugin", "scripts", "ship.py")
CHAL = os.path.join(ROOT, "claude-plugin", "scripts", "challenger.py")

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


# ── a fake Tares: records what lands on /ingest/claude_code, serves memory ───
INGESTED = []
MEMORY = [{"key": "shop", "event_type": "decision", "text": "The shop repo runs tests with make test."},
          {"key": "other", "event_type": "decision", "text": "not this repo"},
          {"key": "shop", "event_type": "observation", "text": "not accepted memory"}]
# ── and a fake xAI chat-completions endpoint for the Grok backend ────────────
XAI = []                                   # (path, authorization, request body) per call
GROK = {"status": 200, "reply": ""}


class FakeTares(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/api/sources/agent_memory/events"):
            body = json.dumps(MEMORY).encode()
            self.send_response(200); self.send_header("content-type", "application/json")
            self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n).decode()
        if self.path == "/v1/chat/completions":
            XAI.append((self.path, self.headers.get("authorization"), json.loads(raw)))
            if GROK["status"] != 200:
                self.send_response(GROK["status"]); self.end_headers(); self.wfile.write(b'{"error":"down"}')
                return
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": GROK["reply"]}}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
            self.send_response(200); self.send_header("content-type", "application/json")
            self.end_headers(); self.wfile.write(body)
            return
        if self.path == "/api/sources":
            self.send_response(409); self.end_headers(); return
        if self.path == "/ingest/claude_code":
            for line in raw.splitlines():
                if line.strip():
                    INGESTED.append(json.loads(line))
            self.send_response(202); self.end_headers(); self.wfile.write(b'{"ingested":1}')
            return
        self.send_response(404); self.end_headers()


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, sys
mode = open(os.environ["FAKE_CODEX_MODE"]).read().strip()
open(os.environ["FAKE_CODEX_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\n")
def emit(o): print(json.dumps(o))
if mode == "fail":
    emit({"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0}})
    emit({"type": "item.completed", "item": {"type": "agent_message",
          "text": "Reviewed.\n- [P1] Null deref in pricing at line 12\n- [P3] Naming nit"}})
elif mode == "pass":
    emit({"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0}})
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Looks good, no findings."}})
elif mode == "inconclusive":
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}})
elif mode == "error":
    sys.stderr.write("auth failure\n"); sys.exit(2)
'''


def run(script, hook, env, args=()):
    p = subprocess.run([sys.executable, script, *args], input=json.dumps(hook), capture_output=True,
                       text=True, env=env, timeout=60)
    try:
        out = json.loads(p.stdout.strip() or "{}") if p.stdout.strip().startswith("{") else p.stdout
    except Exception:
        out = p.stdout
    return out, p


def main():
    scrub_os_environ()
    srv = HTTPServer(("127.0.0.1", 0), FakeTares)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"

    tmp = tempfile.mkdtemp(prefix="tares-plugin-test-")
    data_dir = os.path.join(tmp, "plugin-data")
    repo = os.path.join(tmp, "shop")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "t"], check=True)
    open(os.path.join(repo, "a.txt"), "w").write("a\n")
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "first"], check=True)
    codex = os.path.join(tmp, "codex")
    open(codex, "w").write(FAKE_CODEX); os.chmod(codex, 0o755)
    mode_file, log_file = os.path.join(tmp, "mode"), os.path.join(tmp, "codex.log")
    open(mode_file, "w").write("pass"); open(log_file, "w").close()
    transcript = os.path.join(tmp, "session.jsonl")
    open(transcript, "w").close()

    env = clean_env(CLAUDE_PLUGIN_OPTION_TARES_URL=base, CLAUDE_PLUGIN_DATA=data_dir,
                    CLAUDE_PLUGIN_OPTION_CODEX_BIN=codex, FAKE_CODEX_MODE=mode_file,
                    FAKE_CODEX_LOG=log_file, TARES_CHALLENGER_MAX_LOOPS="3",
                    TARES_CHALLENGER_BACKEND=None, XAI_API_KEY=None, XAI_BASE_URL=None,
                    TARES_GROK_MODEL=None)
    sid = "sess-abc"
    hook = {"session_id": sid, "transcript_path": transcript, "cwd": repo}

    def tline(typ, **msg):
        return json.dumps({"sessionId": sid, "type": typ, "cwd": repo, "timestamp": "2026-08-26T10:00:00Z",
                           "message": msg}) + "\n"

    def codex_calls():
        return [json.loads(l) for l in open(log_file) if l.strip()]

    def set_mode(m):
        open(mode_file, "w").write(m)

    def commit(msg):
        open(os.path.join(repo, "a.txt"), "a").write(msg + "\n")
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-qm", msg], check=True)
        return {**hook, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                "tool_input": {"command": f"git commit -m '{msg}'"},
                "tool_response": {"stdout": f"[main abc] {msg}", "stderr": ""}}

    print("== session start: memory ==")
    o, p = run(SHIP, {**hook, "hook_event_name": "SessionStart", "source": "startup"}, env)
    check("accepted memory for this repo printed as context",
          "make test" in p.stdout and "not this repo" not in p.stdout and "not accepted" not in p.stdout,
          p.stdout[:300] + p.stderr[:300])

    print("== unmarked session ==")
    with open(transcript, "a") as f:
        f.write(tline("user", role="user", content="build a pricing page"))
    run(SHIP, {**hook, "hook_event_name": "UserPromptSubmit"}, env)
    check("plain line shipped without a flow", INGESTED and "flow" not in INGESTED[-1], str(INGESTED[-1:]))
    o, p = run(CHAL, commit("unmarked commit"), env)
    check("commit in an unmarked session: hook says {} and Codex never ran", o == {} and codex_calls() == [], f"{o} {p.stderr[:200]}")

    print("== mark the session ==")
    with open(transcript, "a") as f:
        f.write(tline("assistant", role="assistant", content=[
            {"type": "tool_use", "id": "t1", "name": "mcp__plugin_tares_tares__set_session_flow", "input": {"flow": "challenger"}}]))
        f.write(tline("user", role="user", content=[{"type": "tool_result", "tool_use_id": "t1", "content": "noted"}]))
    run(SHIP, {**hook, "hook_event_name": "PostToolUse", "tool_name": "mcp__tares__set_session_flow"}, env)
    types = [(x.get("type"), x.get("flow")) for x in INGESTED[-3:]]
    check("tool_use line, synthetic session_flow line, then the stamped result line",
          types == [("assistant", "challenger"), ("session_flow", "challenger"), ("user", "challenger")], str(types))
    sys.path.insert(0, os.path.dirname(SHIP))
    from ship import read_flow
    check("marker written for the session", read_flow(data_dir, sid) == "challenger")

    print("== plan challenged ==")
    set_mode("fail")
    o, p = run(CHAL, {**hook, "hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode",
                      "tool_input": {"plan": "# Plan\n1. build it"}}, env)
    ctx = o.get("hookSpecificOutput", {}).get("additionalContext", "") if isinstance(o, dict) else ""
    check("plan review is advisory context with the findings",
          "decision" not in o and "1 blocking" in ctx and "[P1] Null deref" in ctx, str(o)[:300] + p.stderr[:300])
    check("codex ran exec on a plan file", codex_calls()[-1][0] == "exec" and "--skip-git-repo-check" in codex_calls()[-1])
    ev = INGESTED[-1]
    check("challenge_plan shipped with verdict and counts",
          ev["type"] == "challenge_plan" and ev["flow"] == "challenger" and ev["challenge"]["verdict"] == "FAIL"
          and ev["challenge"]["blocking_count"] == 1 and ev["challenge"]["finding_count"] == 2, json.dumps(ev)[:300])

    sys.path.insert(0, os.path.dirname(CHAL))
    import challenger as _c
    check("on a plan only P1 blocks (P2 is advice)",
          _c.counts([{"priority": "P2"}, {"priority": "P1"}], _c.PLAN_BLOCKING) == (2, 1, 0)
          and _c.counts([{"priority": "P2"}], _c.PLAN_BLOCKING) == (1, 0, 0)
          and _c.counts([{"priority": "P2"}]) == (1, 1, 0))

    print("== commit challenged: FAIL blocks ==")
    o, p = run(CHAL, commit("pricing v1"), env)
    check("strict mode blocks on P1", isinstance(o, dict) and o.get("decision") == "block" and "amend" in o.get("reason", ""),
          str(o)[:300] + p.stderr[:300])
    check("codex ran review --commit HEAD", codex_calls()[-1][:2] == ["exec", "--sandbox"] and "review" in codex_calls()[-1]
          and "--commit" in codex_calls()[-1], str(codex_calls()[-1]))
    ev = INGESTED[-1]
    check("challenge_commit shipped with sha, round and counts",
          ev["type"] == "challenge_commit" and ev["challenge"]["verdict"] == "FAIL" and ev["challenge"]["round"] == 1
          and len(ev["challenge"]["sha"]) == 40 and ev["challenge"]["blocking_count"] == 1, json.dumps(ev)[:300])
    gd = os.path.join(repo, ".git")
    check("review recorded in .git history", os.path.exists(os.path.join(gd, "tares-challenger-reviews.jsonl")))
    check("nothing written to the working tree", set(os.listdir(repo)) == {"a.txt", ".git"}, str(os.listdir(repo)))

    print("== stop loop ==")
    o, _ = run(CHAL, {**hook, "hook_event_name": "Stop", "stop_hook_active": False}, env)
    check("stop is blocked while the review is FAIL", o.get("decision") == "block", str(o)[:200])
    o, _ = run(CHAL, {**hook, "hook_event_name": "Stop", "stop_hook_active": True}, env)
    check("second stop still blocked (loop 2 of 3)", o.get("decision") == "block", str(o)[:200])
    o, _ = run(CHAL, {**hook, "hook_event_name": "Stop", "stop_hook_active": True}, env)
    check("third stop hits the cap and lets Claude stop with context",
          "decision" not in o and "max of 3" in o.get("hookSpecificOutput", {}).get("additionalContext", ""), str(o)[:200])
    o, _ = run(CHAL, {**hook, "hook_event_name": "Stop", "stop_hook_active": True}, env)
    check("state cleared after the cap", o == {}, str(o))

    print("== same sha not reviewed twice, PASS clears ==")
    before = len(codex_calls())
    o, _ = run(CHAL, {**hook, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                      "tool_input": {"command": "git commit --amend --no-edit"}, "tool_response": {"stdout": "ok"}}, env)
    check("a retried hook on the same sha does not re-run Codex", len(codex_calls()) == before, str(o)[:200])
    set_mode("pass")
    o, _ = run(CHAL, commit("pricing v2"), env)
    ctx = o.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("PASS comes back as context and asks about pushing", "decision" not in o and "passed" in ctx and "push" in ctx, str(o)[:200])
    o, _ = run(CHAL, {**hook, "hook_event_name": "Stop", "stop_hook_active": False}, env)
    check("stop free after a pass", o == {}, str(o))

    print("== other verdicts never block ==")
    set_mode("inconclusive")
    o, _ = run(CHAL, commit("v3"), env)
    ctx = o.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("inconclusive: context, not a pass, not blocked", "decision" not in o and "INCONCLUSIVE" in ctx, str(o)[:200])
    check("inconclusive shipped as such", INGESTED[-1]["challenge"]["verdict"] == "INCONCLUSIVE")
    set_mode("error")
    o, _ = run(CHAL, commit("v4"), env)
    ctx = o.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("codex error: context with the stderr, not blocked", "decision" not in o and "auth failure" in ctx, str(o)[:200])

    print("== waive ==")
    set_mode("fail")
    o, _ = run(CHAL, commit("v5"), env)
    check("blocked again", o.get("decision") == "block")
    p = subprocess.run([sys.executable, CHAL, "waive", "all"], cwd=repo, capture_output=True, text=True, env=env)
    check("waive all writes the waiver and says so", p.returncode == 0 and "waived: [P1]" in p.stdout, p.stdout + p.stderr)
    check("challenge_waived shipped", INGESTED[-1]["type"] == "challenge_waived" and INGESTED[-1]["challenge"]["findings"][0]["waived"])
    o, _ = run(CHAL, {**hook, "hook_event_name": "Stop", "stop_hook_active": False}, env)
    check("stop free after waiving", o == {}, str(o))
    o, _ = run(CHAL, commit("v6"), env)
    ctx = o.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("next review with the same finding passes with the waiver noted",
          "decision" not in o and "1 finding(s) waived" in ctx, str(o)[:300])

    print("== advise mode ==")
    o, _ = run(CHAL, commit("v7"), {**env, "CLAUDE_PLUGIN_OPTION_CHALLENGER_MODE": "advise"})
    # the waiver still applies, so use a fresh title via a plain non-waived run: switch waivers off
    os.remove(os.path.join(gd, "tares-challenger-waived"))
    o, _ = run(CHAL, commit("v8"), {**env, "CLAUDE_PLUGIN_OPTION_CHALLENGER_MODE": "advise"})
    ctx = o.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("advise mode never blocks", "decision" not in o and "Advise mode" in ctx, str(o)[:200])

    print("== grok backend ==")
    genv = {**env, "CLAUDE_PLUGIN_OPTION_CHALLENGER_BACKEND": "grok", "XAI_API_KEY": "test-key",
            "XAI_BASE_URL": base + "/v1"}
    before = len(codex_calls())
    GROK["reply"] = ("Reviewed.\n[P1] Clicks counted before the 404 check\n  a.txt:3 counts a miss\n"
                     "[P3] Naming nit\nVERDICT: FINDINGS")
    o, p = run(CHAL, commit("grok v1"), genv)
    check("grok findings block in strict mode, named as Grok",
          isinstance(o, dict) and o.get("decision") == "block" and o.get("reason", "").startswith("Grok review")
          and "[P1] Clicks counted" in o.get("reason", ""), str(o)[:300] + p.stderr[:300])
    path, auth, body = XAI[-1]
    user = body["messages"][-1]["content"]
    check("one xAI call with the key, the default model and temperature 0",
          auth == "Bearer test-key" and body["model"] == "grok-4.6" and body["temperature"] == 0, str(body)[:200])
    check("grok is handed the commit message, the patch and the changed file at that commit",
          "grok v1" in user and "+grok v1" in user and "===== a.txt at " in user, user[:400])
    check("codex never ran on the grok backend", len(codex_calls()) == before)
    ev = INGESTED[-1]
    check("challenge_commit names the reviewer and model",
          ev["type"] == "challenge_commit" and ev["challenge"]["verdict"] == "FAIL"
          and ev["challenge"]["reviewer"] == "grok" and ev["challenge"]["model"] == "grok-4.6"
          and ev["challenge"]["blocking_count"] == 1 and ev["challenge"]["finding_count"] == 2, json.dumps(ev)[:300])

    GROK["reply"] = "Every hunk read; nothing wrong.\nVERDICT: CLEAN"
    o, _ = run(CHAL, commit("grok v2"), genv)
    ctx = o.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("an explicit VERDICT: CLEAN passes", "decision" not in o and "passed" in ctx, str(o)[:200])
    o, _ = run(CHAL, {**hook, "hook_event_name": "Stop", "stop_hook_active": False}, genv)
    check("stop free after a grok pass", o == {}, str(o))

    GROK["reply"] = "Looks fine to me."
    o, _ = run(CHAL, commit("grok v3"), genv)
    ctx = o.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("no findings and no verdict line is INCONCLUSIVE, never a pass",
          "decision" not in o and "INCONCLUSIVE" in ctx and INGESTED[-1]["challenge"]["verdict"] == "INCONCLUSIVE",
          str(o)[:200])

    GROK["status"] = 500
    o, _ = run(CHAL, commit("grok v4"), genv)
    ctx = o.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("an xAI error comes back as context, not a block", "decision" not in o and "xAI answered 500" in ctx,
          str(o)[:200])
    GROK["status"] = 200

    # Ashish's review of PR 307: Grok formats findings its own way; none of them may read as clean
    GROK["reply"] = ("Two problems.\n**[P1]** Clicks counted before the 404 check\n"
                     "1. [P2] Counter not reset between tests\n### [P2] Race on the counter\n"
                     "[P1]: Stats leak other links\nVERDICT: FINDINGS")
    o, _ = run(CHAL, commit("grok v6"), genv)
    ev = INGESTED[-1]["challenge"]
    check("bold, numbered, heading and colon findings all parse and block",
          o.get("decision") == "block" and ev["finding_count"] == 4 and ev["blocking_count"] == 4
          and ev["findings"][0]["title"] == "Clicks counted before the 404 check", json.dumps(ev)[:400])

    GROK["reply"] = "P1 - Clicks counted before the 404 check\n(a.txt:3)\nVERDICT: FINDINGS"
    o, _ = run(CHAL, commit("grok v7"), genv)
    ev = INGESTED[-1]["challenge"]
    check("VERDICT: FINDINGS with no parsed P1/P2 fails, never a clean pass",
          o.get("decision") == "block" and ev["verdict"] == "FAIL"
          and "none of its findings could be read" in o.get("reason", "")
          and "P1 - Clicks counted" in o.get("reason", ""), str(o)[:300])

    GROK["reply"] = "[P3] Naming nit\nVERDICT: FINDINGS"
    o, _ = run(CHAL, commit("grok v8"), genv)
    check("VERDICT: FINDINGS with only a P3 parsed still fails",
          o.get("decision") == "block" and INGESTED[-1]["challenge"]["verdict"] == "FAIL", str(o)[:300])

    subprocess.run(["git", "-C", repo, "mv", "a.txt", "renamed.txt"], check=True)
    open(os.path.join(repo, "renamed.txt"), "a").write("edited after the rename\n")
    GROK["reply"] = "Every hunk read; nothing wrong.\nVERDICT: CLEAN"
    subprocess.run(["git", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-C", repo, "commit", "-qm", "grok rename"], check=True)
    run(CHAL, {**hook, "hook_event_name": "PostToolUse", "tool_name": "Bash",
               "tool_input": {"command": "git commit -m 'grok rename'"},
               "tool_response": {"stdout": "[main abc] grok rename", "stderr": ""}}, genv)
    user = XAI[-1][2]["messages"][-1]["content"]
    check("a renamed and edited file is handed over with its body",
          "===== renamed.txt at " in user and "edited after the rename" in user, user[-600:])

    GROK["reply"] = "[P1] No migration for the new column\nVERDICT: FINDINGS"
    o, p = run(CHAL, {**hook, "hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode",
                      "tool_input": {"plan": "# Plan\n1. add clicks"}}, genv)
    ctx = o.get("hookSpecificOutput", {}).get("additionalContext", "") if isinstance(o, dict) else ""
    check("grok plan review is advisory context with the finding",
          "decision" not in o and ctx.startswith("Grok challenged the plan") and "[P1] No migration" in ctx,
          str(o)[:300] + p.stderr[:300])
    check("the plan text reached grok", "1. add clicks" in XAI[-1][2]["messages"][-1]["content"])
    o, _ = run(CHAL, {**hook, "hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode",
                      "tool_input": {"plan": "# Plan\n" + "x" * 300_000}}, genv)
    sent = XAI[-1][2]["messages"][-1]["content"]
    check("a huge plan is capped before it is sent", len(sent) < 130_000 and "[plan truncated at" in sent,
          str(len(sent)))
    ev = INGESTED[-1]
    check("challenge_plan names the reviewer", ev["type"] == "challenge_plan" and ev["challenge"]["reviewer"] == "grok")

    calls = len(XAI)
    nokey = {k: v for k, v in genv.items() if k != "XAI_API_KEY"}
    o, _ = run(CHAL, {**hook, "hook_event_name": "PostToolUse", "tool_name": "ExitPlanMode",
                      "tool_input": {"plan": "# Plan"}}, nokey)
    ctx = o.get("hookSpecificOutput", {}).get("additionalContext", "")
    check("no xAI key: the plan hook says so instead of calling xAI", "xAI key is not set" in ctx and len(XAI) == calls,
          str(o)[:200])
    o, _ = run(CHAL, commit("grok v5"), nokey)
    check("no xAI key: commits are not reviewed", o == {} and len(XAI) == calls, str(o)[:200])

    set_mode("pass")
    o, _ = run(CHAL, commit("codex again"), env)
    check("without the option codex stays the reviewer",
          len(codex_calls()) == before + 1 and INGESTED[-1]["challenge"]["reviewer"] == "codex", str(o)[:200])

    print("== grok timeouts ==")
    sys.path.insert(0, os.path.dirname(CHAL))
    import socket
    import urllib.error
    import grok
    real, os.environ["XAI_API_KEY"] = grok.urllib.request.urlopen, "test-key"
    try:
        for label, exc in (("a read timeout", TimeoutError("read")),
                           ("a connect timeout", urllib.error.URLError(socket.timeout("timed out"))),
                           ("a refused connection", urllib.error.URLError(ConnectionRefusedError(61, "refused")))):
            def boom(*a, _e=exc, **k):
                raise _e
            grok.urllib.request.urlopen = boom
            r = grok.run_grok("x", 5)
            want = "ERROR" if "refused" in label else "TIMEOUT"
            check(f"{label} is reported as {want}", r["verdict"] == want, str(r))
    finally:
        grok.urllib.request.urlopen = real
        os.environ.pop("XAI_API_KEY", None)

    print("== session end ==")
    run(SHIP, {**hook, "hook_event_name": "SessionEnd"}, env)
    check("session_end line shipped with the flow", INGESTED[-1]["type"] == "session_end" and INGESTED[-1]["flow"] == "challenger",
          json.dumps(INGESTED[-1]))
    check("marker removed at session end", read_flow(data_dir, sid) == "")
    o, _ = run(CHAL, commit("after end"), env)
    check("hooks inert again after the session ended", o == {}, str(o))

    print("== flow off mid-session ==")
    sid2 = "sess-2"
    hook2 = {**hook, "session_id": sid2}
    with open(transcript, "a") as f:
        f.write(tline("assistant", role="assistant", content=[
            {"type": "tool_use", "id": "t2", "name": "mcp__tares__set_session_flow", "input": {"flow": "challenger"}}]).replace(sid, sid2))
    run(SHIP, {**hook2, "hook_event_name": "PostToolUse"}, env)
    check("second session marked", read_flow(data_dir, sid2) == "challenger")
    with open(transcript, "a") as f:
        f.write(tline("assistant", role="assistant", content=[
            {"type": "tool_use", "id": "t3", "name": "mcp__tares__set_session_flow", "input": {"flow": ""}}]).replace(sid, sid2))
    run(SHIP, {**hook2, "hook_event_name": "PostToolUse"}, env)
    check("flow cleared by an empty set_session_flow", read_flow(data_dir, sid2) == "")
    check("cleared session_flow line shipped without a flow stamp",
          INGESTED[-1]["type"] == "session_flow" and INGESTED[-1].get("flow", "") == "", json.dumps(INGESTED[-1]))

    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
