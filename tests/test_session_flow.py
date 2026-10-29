"""TR-195: the set_session_flow MCP tool only acknowledges, and the claude_code connector turns a
session_flow line (and a `flow` stamp on any line) into a `flow` label triggers and views can use.
TR-196: challenge_plan / challenge_commit / challenge_waived lines carry verdict, sha and counts as labels."""
import asyncio
from types import SimpleNamespace

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))


def main():
    from tares.connectors.claude_code import ClaudeCodeConnector
    from tares import mcp_server

    cfg = SimpleNamespace(name="claude_code", type="claude_code", config={})
    c = ClaudeCodeConnector(cfg, store=None)
    flow_line = {"sessionId": "s1", "type": "session_flow", "flow": "challenger",
                 "cwd": "/tmp/proj", "timestamp": "2026-08-26T10:00:00Z"}
    envs = c.map_payload([flow_line])
    ck("session_flow line maps to one event", len(envs) == 1)
    e = envs[0]
    ck("event_type is session_flow", e.event_type == "session_flow", e.event_type)
    ck("flow label set", e.labels.get("flow") == "challenger", str(e.labels))
    ck("text names the flow", e.text == "session flow: challenger", e.text)

    stamped = {"sessionId": "s1", "type": "assistant", "flow": "challenger", "cwd": "/tmp/proj",
               "timestamp": "2026-08-26T10:00:01Z",
               "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}}
    e = c.map_payload(stamped)[0]
    ck("flow stamp on an ordinary line becomes a label", e.labels.get("flow") == "challenger")
    ck("ordinary line keeps its type", e.event_type == "assistant")

    plain = {"sessionId": "s1", "type": "user", "cwd": "/tmp/proj",
             "message": {"role": "user", "content": "hello"}}
    e = c.map_payload(plain)[0]
    ck("no flow, no label", "flow" not in e.labels, str(e.labels))
    cleared = c.map_payload({"sessionId": "s1", "type": "session_flow", "flow": ""})[0]
    ck("cleared flow renders", cleared.text == "session flow: cleared", cleared.text)
    ck("flow is a declared axis", any(p["name"] == "flow" for p in ClaudeCodeConnector.PROVIDES))

    commit = {"sessionId": "s1", "type": "challenge_commit", "flow": "challenger", "cwd": "/tmp/proj",
              "timestamp": "2026-08-26T10:05:00Z",
              "challenge": {"verdict": "fail", "sha": "abcdef1234567890", "round": 2,
                            "finding_count": 2, "blocking_count": 1, "waived_count": 0,
                            "duration_seconds": 41.5, "prose": "token ghp_abcdefghijklmnopqrstuvwxyz",
                            "findings": [{"priority": "P1", "title": "Null deref in pricing", "waived": False},
                                         {"priority": "P3", "title": "Naming", "waived": False}]}}
    e = c.map_payload(commit)[0]
    ck("challenge_commit keeps its type", e.event_type == "challenge_commit", e.event_type)
    ck("verdict label uppercased", e.labels.get("verdict") == "FAIL", str(e.labels))
    ck("sha label shortened", e.labels.get("sha") == "abcdef123456", str(e.labels))
    ck("numeric challenge labels", e.labels.get("blocking_count") == 1 and e.labels.get("round") == 2
       and e.labels.get("duration_seconds") == 41.5, str(e.labels))
    ck("flow label rides along", e.labels.get("flow") == "challenger")
    ck("text summarizes the review",
       e.text.startswith("Challenger reviewed commit abcdef12, round 2: 2 findings (1 blocking)\n[P1] Null deref"), e.text)
    ck("prose in payload is redacted", "ghp_" not in str(e.payload), str(e.payload)[:200])

    plan = {"sessionId": "s1", "type": "challenge_plan", "cwd": "/tmp/proj",
            "challenge": {"verdict": "PASS", "plan": "pricing-page.md", "finding_count": 0,
                          "blocking_count": 0, "duration_seconds": 12, "findings": []}}
    e = c.map_payload(plan)[0]
    ck("challenge_plan text", e.text == "Challenger reviewed the plan pricing-page.md: no findings", e.text)
    graded = {**commit, "challenge": {**commit["challenge"], "reviewer": "grok", "model": "grok-4.6"}}
    e = c.map_payload(graded)[0]
    ck("text names the reviewer when the plugin sends one",
       e.text.startswith("Challenger (Grok) reviewed commit abcdef12, round 2"), e.text)
    waived = {"sessionId": "s1", "type": "challenge_waived", "cwd": "/tmp/proj",
              "challenge": {"findings": [{"priority": "P2", "title": "Missing test"}]}}
    e = c.map_payload(waived)[0]
    ck("challenge_waived text", e.text == "Challenger finding waived\n[P2] Missing test", e.text)

    tools = asyncio.run(mcp_server.mcp.list_tools())
    ck("set_session_flow is an MCP tool", any(t.name == "set_session_flow" for t in tools))
    ack = asyncio.run(mcp_server.set_session_flow("challenger"))
    ck("ack names the flow", "challenger" in ack, ack)
    ack = asyncio.run(mcp_server.set_session_flow(""))
    ck("empty flow acks a clear", "clear" in ack, ack)

    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


if __name__ == "__main__":
    main()
