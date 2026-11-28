"""A click on a Slack button Tares posted (POST /api/slack/events, TR-1809).

Every button Tares posts now is a link: "Open in Tares" on a waiting item (tares/waiting_slack.py)
and the link buttons on a finding. Slack still sends the click as a signed `block_actions` request,
and wants a 200 within 3 seconds. Checked here, against the real app with a stub response_url:

  - the waiting item's own "Open in Tares" button, as waiting_slack.message builds it: 200, and
    nothing is posted back, because the browser already went where it points;
  - a link button known by its action id alone (no url in the payload): the same;
  - any other button (an incident or "Worth raising" button on an old message): 200, and the clicker
    alone is told the button no longer does anything (slack.GONE_NOTE), never replacing the message;
  - such a click with no response_url, and an interaction that is not a block action: 200, nothing
    posted, nothing raised;
  - a click with a bad signature: 401, nothing posted.

Run: .venv/bin/python tests/test_slack_interactivity.py
"""
import asyncio
import hashlib
import hmac
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-slack-interactivity-")
os.environ["TARES_HOME"] = os.path.join(_TMP, "api")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PUBLIC_URL"] = "https://cell.example.com"
SECRET = "slack-signing-secret-interactivity"
os.environ["TARES_SLACK_SIGNING_SECRET"] = SECRET

import httpx

from tares import slack as slack_mod
from tares import waiting_slack as ws

P = F = 0


def ck(label, cond, detail: object = ""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# ── a stub for Slack's response_url: records every body posted to it ──
REPLIES: list[dict] = []


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        REPLIES.append(json.loads(self.rfile.read(n) or b"{}"))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
server = HTTPServer(("127.0.0.1", PORT), Hook)
threading.Thread(target=server.serve_forever, daemon=True).start()
RESPONSE_URL = f"http://127.0.0.1:{PORT}/hook"


def signed(payload: dict, secret: str = SECRET) -> tuple[bytes, dict]:
    raw = urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"content-type": "application/x-www-form-urlencoded",
                 "x-slack-request-timestamp": ts, "x-slack-signature": sig}


def click(action: dict, response_url: str | None = RESPONSE_URL, kind: str = "block_actions") -> dict:
    p = {"type": kind, "user": {"id": "U_ALEX", "name": "alex"}, "actions": [action],
         "container": {"channel_id": "D1", "message_ts": "1.1"}, "channel": {"id": "D1"}}
    if response_url:
        p["response_url"] = response_url
    return p


async def settle(want: int, timeout: float = 3.0) -> None:
    """Wait for `want` replies, or the timeout (for a check that nothing came, the whole of it)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end and len(REPLIES) < want:
        await asyncio.sleep(0.05)


async def main():
    from tares.daemon import make_app
    app = make_app()
    cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=15)

    async def post(payload: dict, secret: str = SECRET):
        raw, headers = signed(payload, secret)
        return await cx.post("/api/slack/events", content=raw, headers=headers)

    print("== the waiting item's Open in Tares button ==")
    msg = ws.message("U_ALEX", {"id": "w1", "ask": "Answer the question on T3"},
                     "https://cell.example.com/?item=w1")
    actions = next(b for b in msg["blocks"] if b.get("type") == "actions")
    button = dict(actions["elements"][0])
    ck("the message carries the link button", button.get("url") and button.get("action_id") == "open_item",
       button)
    action = {**button, "block_id": "b1", "action_ts": "2.2"}
    r = await post(click(action))
    ck("the click is answered 200", r.status_code == 200, (r.status_code, r.text[:200]))
    await settle(1)
    ck("nothing is posted back for a link click", REPLIES == [], REPLIES)

    print("== a link button known by its action id alone ==")
    REPLIES.clear()
    r = await post(click({"type": "button", "action_id": "open_item", "block_id": "b1"}))
    ck("200", r.status_code == 200, r.status_code)
    r = await post(click({"type": "button", "action_id": "tares_link_0", "block_id": "b1"}))
    ck("200 for a finding's link button too", r.status_code == 200, r.status_code)
    await settle(1)
    ck("nothing is posted back", REPLIES == [], REPLIES)

    print("== any other button: an old message's ==")
    REPLIES.clear()
    old = {"type": "button", "action_id": "tares_incident_on_it", "block_id": "b2",
           "value": json.dumps({"i": "inc_1", "w": None})}
    r = await post(click(old))
    ck("the click is answered 200", r.status_code == 200, (r.status_code, r.text[:200]))
    await settle(1)
    ck("one reply is posted to the response_url", len(REPLIES) == 1, REPLIES)
    reply = REPLIES[0] if REPLIES else {}
    ck("it says the button no longer does anything", reply.get("text") == slack_mod.GONE_NOTE, reply)
    ck("only the clicker sees it", reply.get("response_type") == "ephemeral", reply)
    ck("the clicked message stays as it is", reply.get("replace_original") is False, reply)
    ck("the words have no em dash", "\u2014" not in slack_mod.GONE_NOTE, slack_mod.GONE_NOTE)

    REPLIES.clear()
    r = await post(click({"type": "button", "action_id": "tares_worth_yes", "block_id": "b3",
                          "value": json.dumps({"r": "run_1"})}))
    await settle(1)
    ck("a Worth raising button gets the same answer", r.status_code == 200 and len(REPLIES) == 1
       and REPLIES[0].get("text") == slack_mod.GONE_NOTE, (r.status_code, REPLIES))

    print("== another workspace's button ==")
    REPLIES.clear()
    r = await post(click({"type": "button", "action_id": "tares_incident_ack", "block_id": "b4",
                          "value": json.dumps({"i": "inc_1", "w": "some-other-cell"})}))
    await settle(1, timeout=1.5)
    ck("a button another workspace's cell made stays silent (shared Slack app)", r.status_code == 200
       and REPLIES == [], (r.status_code, REPLIES))

    print("== nothing to answer to ==")
    REPLIES.clear()
    r = await post(click(old, response_url=None))
    ck("a click with no response_url is answered 200", r.status_code == 200, r.status_code)
    r = await post(click(old, kind="view_submission"))
    ck("an interaction that is not a block action is answered 200", r.status_code == 200, r.status_code)
    r = await post(click(old), secret="not-the-secret")
    ck("a bad signature is refused", r.status_code == 401, r.status_code)
    await settle(1, timeout=1.5)
    ck("and nothing is posted for any of them", REPLIES == [], REPLIES)

    await cx.aclose()


asyncio.run(main())
server.shutdown()
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
