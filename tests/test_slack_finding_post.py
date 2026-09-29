"""An agent's finding posted by the workspace bot (TR-275): the channel message carries the entity,
fields, an excerpt and buttons; a long note's full text goes in that message's thread; a short note
is posted whole. Against a stub Slack API that records what it was sent.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PORT = 8866
os.environ["TARES_SLACK_API_BASE"] = f"http://127.0.0.1:{PORT}"
os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-test"
os.environ["TARES_PUBLIC_URL"] = "https://cell.example.com"

import tares.builtin_agents as ba  # noqa: E402  (reads the Slack API base at import)

PASS = FAIL = 0
POSTS = []
REJECT = {"on": False}


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {label}")
    else:
        FAIL += 1; print(f"  FAIL {label}  {detail}")


class Slack(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        POSTS.append(body)
        out = ({"ok": False, "error": "channel_not_found"} if REJECT["on"]
               else {"ok": True, "channel": body.get("channel"), "ts": f"1790000000.{len(POSTS):06d}"})
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class Store:
    def get_setting(self, k):
        return None


LONG = ("## 1. What is failing\n\nkonnectivity-agent log volume rose 3.4x in ten minutes. " * 20
        + "\n\n**Summary:** Benign traffic growth, no action needed.")
META = {"verdict": "resolved", "model": "claude-sonnet-5", "run_id": "run_9",
        "when": "2026-09-29T07:30:51+00:00"}


async def main():
    threading.Thread(target=HTTPServer(("127.0.0.1", PORT), Slack).serve_forever, daemon=True).start()
    r = object.__new__(ba.AgentRunner)
    r.store = Store()

    print("== a long note: the message, then the full note in its thread ==")
    ok = await r._slack_channel("rca", "C123", "escalate", "konnectivity-agent", LONG, META)
    check("delivered", ok is True)
    check("two posts", len(POSTS) == 2, str(len(POSTS)))
    top, reply = POSTS[0], POSTS[1] if len(POSTS) > 1 else {}
    kinds = [b["type"] for b in top.get("blocks", [])]
    check("channel message: header, fields, excerpt, buttons, context",
          kinds == ["header", "section", "section", "actions", "context"], str(kinds))
    check("the excerpt is the note's summary",
          top["blocks"][2]["text"]["text"].startswith("*Summary:* Benign"), top["blocks"][2]["text"]["text"])
    check("the reply is in the message's thread", reply.get("thread_ts") == "1790000000.000001", str(reply)[:200])
    check("the reply carries the full note", "rose 3.4x" in json.dumps(reply.get("blocks")))
    check("neither unfurls links", top.get("unfurl_links") is False and reply.get("unfurl_links") is False)
    check("the notification text names the agent and the entity",
          top["text"].startswith("rca on konnectivity-agent:"), top["text"])

    print("== a short note: posted whole, no thread ==")
    POSTS.clear()
    ok = await r._slack_channel("rca", "C123", "escalate", "api", "Traffic is normal.", META)
    check("one post", ok is True and len(POSTS) == 1, str(len(POSTS)))
    check("the whole note is in it, and no pointer to a thread",
          any(b.get("text", {}).get("text") == "Traffic is normal." for b in POSTS[0]["blocks"])
          and "thread" not in json.dumps(POSTS[0]["blocks"][-1]), json.dumps(POSTS[0]["blocks"])[:300])

    print("== Slack refuses the post ==")
    POSTS.clear()
    REJECT["on"] = True
    ok = await r._slack_channel("rca", "C404", "escalate", "api", LONG, META)
    check("not delivered, and no thread reply attempted", ok is False and len(POSTS) == 1, str(len(POSTS)))


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
