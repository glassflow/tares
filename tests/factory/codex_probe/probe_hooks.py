"""Which of the Claude plugin's hook events does Codex fire, with what payload, and does it take the answers
the challenger gives? A by-hand probe (TR-1576), not in CI: it needs a signed-in `codex`.

    TARES_PROBE_CODEX=/path/to/codex python tests/factory/codex_probe/probe_hooks.py [--out DIR]

claude-plugin/hooks/hooks.json registers `ship` (the recorder) on SessionStart, UserPromptSubmit, PostToolUse,
Stop, SubagentStop, PermissionRequest, Notification and SessionEnd, and `challenger` on PostToolUse(Bash) and
Stop. Here one stored thread runs with a hook on every event Codex names that writes down its payload, plus two
hooks shaped like the challenger's answers: a PostToolUse that blocks after a `git commit` with a finding, and a
Stop that sends the model back once ("decision": "block"). The probe records which events fired, the fields
of each payload, whether `transcript_path` names a file that exists, and whether the two answers were obeyed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appserver import AppServer, codex_bin, items, notifications  # noqa: E402
from fixture import World  # noqa: E402
from probe_guard import EFFORT, MODEL, QUIET, RULES  # noqa: E402

EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest", "PostToolUse", "Stop",
          "SubagentStart", "SubagentStop", "PreCompact", "PostCompact", "SessionEnd", "Interrupt", "Notification"]

LOGGER = r'''
import json, os, sys
raw = sys.stdin.read()
try:
    data = json.loads(raw)
except ValueError:
    data = {"unparsed": raw[:200]}
data["_transcript_exists"] = bool(data.get("transcript_path")) and os.path.exists(data["transcript_path"])
with open(os.environ["PROBE_HOOK_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(data) + "\n")
event = data.get("hook_event_name")
state = os.environ["PROBE_HOOK_LOG"] + ".state"
if event == "PostToolUse" and "git commit" in json.dumps(data.get("tool_input") or ""):
    print(json.dumps({"decision": "block", "reason": "CHALLENGER-FINDING-P1: the commit has no test. Say the words CHALLENGER-FINDING-P1 in your final answer."}))
elif event == "Stop" and not os.path.exists(state):
    open(state, "w").close()
    print(json.dumps({"decision": "block", "reason": "Run `echo fix-loop` once more, then stop."}))
elif event in ("Stop", "SubagentStop", "SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact"):
    print("{}")
'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "codex-probe-out"))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    w = World()
    logger = os.path.join(w.root, "hook_logger.py")
    with open(logger, "w", encoding="utf-8") as f:
        f.write(LOGGER)
    hook_log = os.path.join(w.root, "hooks.jsonl")
    hooks = {ev: [{"hooks": [{"type": "command", "command": f"{sys.executable} {logger}", "timeout": 3 if ev in ("SessionEnd", "Interrupt") else 10}]}]
             for ev in EVENTS}
    app = AppServer.spawn(codex_bin(), args=QUIET, cwd=w.work, env=w.env("builder", extra={"PROBE_HOOK_LOG": hook_log}),
                          transcript_path=os.path.join(args.out, "hooks.wire.jsonl"), stderr_path=os.path.join(args.out, "hooks.stderr.log"))
    app.initialize()
    started = app.request("thread/start", {"cwd": w.work, "config": {"hooks": hooks, "bypass_hook_trust": True},
                                           "approvalPolicy": "never", "sandbox": "danger-full-access", "model": MODEL,
                                           "developerInstructions": RULES})
    thread = started["thread"]["id"]
    listed = app.request("hooks/list", {"threadId": thread})
    accepted = sorted({h["eventName"] for d in listed.get("data", []) for h in d.get("hooks", [])})
    warnings = [x for d in listed.get("data", []) for x in d.get("warnings", []) + [e.get("message") for e in d.get("errors", [])]]
    turn = app.run_turn(thread, "Step 1: run `git commit --allow-empty -m probe`. Step 2: run `echo done`.", timeout=240, effort=EFFORT)
    final = [i.get("text") for i in items(app.log, kind="agentMessage") if i.get("phase") == "final_answer"]
    cmds = [i.get("command") for i in items(app.log, kind="commandExecution")]
    for method in ("thread/archive", "thread/delete"):
        try:
            app.request(method, {"threadId": thread}, timeout=20)
        except Exception:
            pass
    app.close()
    payloads = []
    try:
        with open(hook_log, encoding="utf-8") as f:
            payloads = [json.loads(line) for line in f if line.strip()]
    except OSError:
        pass
    by_event: dict[str, dict] = {}
    for p in payloads:
        ev = p.get("hook_event_name") or "?"
        e = by_event.setdefault(ev, {"count": 0, "fields": set(), "transcript_exists": None})
        e["count"] += 1
        e["fields"].update(k for k in p if not k.startswith("_"))
        if p.get("transcript_path"):
            e["transcript_exists"] = bool(p.get("_transcript_exists"))   # looked at when the hook ran
    fired = {ev: {"count": e["count"], "fields": sorted(e["fields"]), "transcript_path_names_a_file": e["transcript_exists"]}
             for ev, e in by_event.items()}
    post = next((p for p in payloads if p.get("hook_event_name") == "PostToolUse"), {})
    result = {
        "events_configured": EVENTS, "events_codex_accepted": accepted, "config_warnings": warnings,
        "events_fired": fired, "events_never_fired": [ev for ev in EVENTS if ev not in fired],
        "post_tool_use_response_shape": sorted((post.get("tool_response") or {}).keys()) if isinstance(post.get("tool_response"), dict) else type(post.get("tool_response")).__name__,
        "turn": turn["status"], "commands": cmds, "final_answers": [(t or "")[:300] for t in final],
        "model_repeated_the_finding": any("CHALLENGER-FINDING-P1" in (t or "") for t in final),
        "checks": {
            "PostToolUse block was taken (Codex reports the hook run as blocked, with the finding as feedback)":
                any(n.get("run", {}).get("eventName") == "postToolUse" and n["run"].get("status") == "blocked"
                    and any("CHALLENGER-FINDING-P1" in (x.get("text") or "") for x in n["run"].get("entries", []))
                    for n in notifications(app.log, "hook/completed")),
            "Stop block sent the model back once (echo fix-loop ran)": any("fix-loop" in (c or "") for c in cmds),
            "the recorder's events fired (SessionStart, UserPromptSubmit, PostToolUse, Stop)":
                all(ev in fired for ev in ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")),
            "payloads carry session_id and cwd": all({"session_id", "cwd"} <= set(e["fields"]) for e in fired.values()),
        }}
    result["pass"] = all(result["checks"].values())
    with open(os.path.join(args.out, "hooks.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=1, ensure_ascii=False)
    print("PASS" if result["pass"] else "FAIL", "hook events and challenger answers")
    for k, v in result["checks"].items():
        print(f"      {'ok ' if v else 'NO '} {k}")
    print("      fired:", {k: v["count"] for k, v in fired.items()})
    print("      never fired:", result["events_never_fired"])
    print("      accepted by Codex:", accepted, warnings[:3])
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
