"""The inbound half of Slack: `POST /api/slack/events` and the `/tares ask …` slash command.

End to end against a real daemon, a stub Anthropic endpoint (streaming, because the Ask path
streams) and a stub `response_url` standing in for Slack's reply hook.

The three contracts this endpoint lives or dies by are each asserted directly:

· **Signatures.** It is the only route public to the auth middleware that takes a body from the
  internet. A tampered body, a forged signature, a stale timestamp and a missing signature are all
  401 — and crucially the model is never called for any of them. With no signing secret configured
  it is 503, never 200.
· **Slack's 3-second ACK.** The command answers immediately and the model's answer arrives later
  on `response_url`, in-thread when the command came from a thread.
· **Never silence.** No Anthropic key, or hitting the daily cap, comes back as words a user can
  act on.

The HMAC here is computed independently of `tares.slack_verify` (its own vectors live in
tests/test_slack_verify.py) — the server and the test must agree on the wire format, not on an
implementation.
"""
import asyncio, hashlib, hmac, json, os, signal, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

SEED = "/tmp/slack_ask_catalog.yaml"
with open(SEED, "w") as fh:
    fh.write(
        "sources:\n  - name: evt\n    connector: webhook\n    poll: 5s\n"
        "    config:\n      labels:\n        - name: service\n          field: service\n"
        "          primary: true\n"
        "views:\n  - name: svc\n    key_field: service\n    sources: [evt]\n")

DB, PORT, STUB_PORT = "/tmp/slack_ask.duckdb", "8814", "8815"
SECRET = "test-signing-secret-0123456789"   # gitleaks:allow — a test fixture, not a credential
WRONG_SECRET = "not-the-signing-secret"
AUTH = "test-auth-token"
ANSWER = "**checkout-svc** logged 42 errors after the 14:02 deploy. See [the timeline](https://x/y)."
B = f"http://127.0.0.1:{PORT}"
RESPONSE_URL = f"http://127.0.0.1:{STUB_PORT}/response"

MODEL_CALLS: list = []
REPLIES: list = []


class Stub(BaseHTTPRequestHandler):
    """/v1/messages — the Anthropic streaming API the Ask path talks to.
    /response      — Slack's response_url, where the answer must land."""

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        if self.path.startswith("/response"):
            REPLIES.append(json.loads(raw))
            return self._send(b'{"ok":true}', "application/json")
        MODEL_CALLS.append(json.loads(raw or b"{}"))
        self._send(_sse_answer(), "text/event-stream")

    def _send(self, raw: bytes, ctype: str):
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def _sse_answer() -> bytes:
    """One text-only assistant turn as Anthropic server-sent events."""
    ev = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-4-6",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": ANSWER}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 20}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in ev).encode()


# ── signing, computed here rather than imported: the wire format is the contract ──
def sign(ts: str, body: bytes, secret: str = SECRET) -> str:
    base = f"v0:{ts}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def signed_headers(body: bytes, ctype: str, ts: str | None = None, secret: str = SECRET,
                   sig: str | None = None) -> dict:
    ts = ts or str(int(time.time()))
    return {"content-type": ctype, "x-slack-request-timestamp": ts,
            "x-slack-signature": sig or sign(ts, body, secret)}


def command_body(text: str, user: str = "U1", thread_ts: str | None = None,
                 response_url: str = RESPONSE_URL) -> bytes:
    form = {"token": "deprecated", "team_id": "T1", "user_id": user, "channel_id": "C1",
            "command": "/tares", "text": text, "response_url": response_url}
    if thread_ts:
        form["thread_ts"] = thread_ts
    return urlencode(form).encode()


async def post_raw(cx, body: bytes, headers: dict, url: str = f"{B}/api/slack/events"):
    return await cx.post(url, content=body, headers=headers)


async def slash(cx, text, **kw):
    body = command_body(text, **{k: v for k, v in kw.items() if k in ("user", "thread_ts", "response_url")})
    return await post_raw(cx, body, signed_headers(body, "application/x-www-form-urlencoded"))


async def _wait(url, tries=80):
    for _ in range(tries):
        try:
            async with httpx.AsyncClient() as cx:
                if (await cx.get(url, timeout=1)).status_code == 200:
                    return True
        except Exception:
            pass
        await asyncio.sleep(0.25)
    return False


async def _until(fn, tries=60):
    for _ in range(tries):
        if fn():
            return True
        await asyncio.sleep(0.5)
    return False


def _spawn(env):
    return subprocess.Popen([sys.executable, "-c", "from tares.cli import run_daemon; run_daemon()"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _stop(proc):
    proc.send_signal(signal.SIGTERM)
    try: proc.wait(timeout=5)
    except Exception: proc.kill()


async def main():
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)

    stub = ThreadingHTTPServer(("127.0.0.1", int(STUB_PORT)), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    base_env = {**os.environ, "TARES_DB": DB, "TARES_CATALOG": SEED, "TARES_PORT": PORT,
                "TARES_OTLP_GRPC_PORT": "off",
                "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{STUB_PORT}"}
    for k in ("TARES_SLACK_SIGNING_SECRET", "TARES_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
        base_env.pop(k, None)

    # ── phase 1: no signing secret configured — 503, never 200 ──────────────
    # An unverifiable request must not be served at all. This is the failure mode that would
    # silently turn the endpoint into an open, unauthenticated command runner.
    proc = _spawn(base_env)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (phase 1)", False); return
        async with httpx.AsyncClient(timeout=20) as cx:
            st = (await cx.get(f"{B}/api/settings/slack-signing-secret")).json()
            ck("no signing secret configured to start with", st["configured"] is False, str(st))

            body = command_body("ask anything")
            r = await post_raw(cx, body, signed_headers(body, "application/x-www-form-urlencoded"))
            ck("a correctly signed command with no secret configured -> 503",
               r.status_code == 503, f"{r.status_code} {r.text[:200]}")
            ck("...and says how to configure it", "SIGNING_SECRET" in r.text, r.text[:200])
            r = await post_raw(cx, body, {"content-type": "application/x-www-form-urlencoded"})
            ck("an unsigned request with no secret configured -> 503 (not 200)",
               r.status_code == 503, str(r.status_code))
            ck("nothing was executed", not MODEL_CALLS, str(MODEL_CALLS)[:200])

            # the secret is a credential: write-only, exactly like the bot token
            r = await cx.put(f"{B}/api/settings/slack-signing-secret", json={"secret": "xoxb-nope"})
            ck("a bot token pasted as the signing secret is rejected", r.status_code == 400, r.text[:200])
            r = await cx.put(f"{B}/api/settings/slack-signing-secret", json={"secret": "stored-secret"})
            ck("PUT signing secret -> 200", r.status_code == 200, r.text)
            ck("PUT never echoes the secret", "stored-secret" not in r.text, r.text)
            st = (await cx.get(f"{B}/api/settings/slack-signing-secret")).json()
            ck("the secret is never returned", "stored-secret" not in json.dumps(st), str(st))
            ck("reported configured from the console",
               st["configured"] and st["source"] == "console", str(st))

            body = command_body("ask anything")
            r = await post_raw(cx, body, signed_headers(body, "application/x-www-form-urlencoded",
                                                        secret="stored-secret"))
            ck("a command signed with the STORED secret is accepted", r.status_code == 200,
               f"{r.status_code} {r.text[:200]}")
            r = await cx.delete(f"{B}/api/settings/slack-signing-secret")
            ck("DELETE clears the stored secret", r.status_code == 200, r.text)
    finally:
        _stop(proc)
    REPLIES.clear(); MODEL_CALLS.clear()

    # ── phase 2: signed, keyed, capped ──────────────────────────────────────
    env2 = {**base_env, "TARES_SLACK_SIGNING_SECRET": SECRET, "ANTHROPIC_API_KEY": "sk-test",
            "TARES_SLACK_DAILY_CAP": "2"}
    proc = _spawn(env2)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (phase 2)", False); return
        async with httpx.AsyncClient(timeout=30) as cx:
            st = (await cx.get(f"{B}/api/settings/slack-signing-secret")).json()
            ck("the environment beats the stored secret",
               st["source"] == "env:TARES_SLACK_SIGNING_SECRET" and st["env_overrides"] is True,
               str(st))

            # ── the handshake, without which the app can never be configured ──
            hs = json.dumps({"type": "url_verification", "challenge": "abc123", "token": "x"}).encode()
            r = await post_raw(cx, hs, signed_headers(hs, "application/json"))
            ck("url_verification handshake -> 200", r.status_code == 200, r.text[:200])
            ck("...echoes the challenge", r.json().get("challenge") == "abc123", r.text[:200])
            r = await post_raw(cx, hs, {"content-type": "application/json"})
            ck("an UNSIGNED handshake is rejected", r.status_code == 401, str(r.status_code))

            # ── the negative cases: nothing runs, nothing leaks ──────────────
            good = command_body("ask what happened to checkout-svc")
            hdr = signed_headers(good, "application/x-www-form-urlencoded")

            r = await post_raw(cx, command_body("ask something else"), hdr)
            ck("a tampered body is rejected (401)", r.status_code == 401, str(r.status_code))
            r = await post_raw(cx, good, {**hdr, "x-slack-signature": "v0=" + "0" * 64})
            ck("a forged signature is rejected (401)", r.status_code == 401, str(r.status_code))
            r = await post_raw(cx, good, signed_headers(good, "application/x-www-form-urlencoded",
                                                        secret=WRONG_SECRET))
            ck("a signature from the wrong secret is rejected (401)", r.status_code == 401,
               str(r.status_code))
            stale = str(int(time.time()) - 600)
            r = await post_raw(cx, good, signed_headers(good, "application/x-www-form-urlencoded",
                                                        ts=stale))
            ck("a replayed request (10 min old) is rejected (401)", r.status_code == 401,
               str(r.status_code))
            r = await post_raw(cx, good, {"content-type": "application/x-www-form-urlencoded"})
            ck("an unsigned request is rejected (401)", r.status_code == 401, str(r.status_code))
            ck("the rejection does not explain itself to the caller",
               "signature" in r.text and "timestamp" not in r.text, r.text[:200])
            ck("no rejected request reached the model", not MODEL_CALLS, str(MODEL_CALLS)[:200])
            ck("no rejected request produced a Slack reply", not REPLIES, str(REPLIES)[:200])

            # ── usage: a malformed command is a message, not a stack trace ───
            r = await slash(cx, "")
            ck("an empty command -> 200 with usage", r.status_code == 200 and "usage" in r.text,
               r.text[:200])
            ck("...ephemeral, so it isn't broadcast to the channel",
               r.json().get("response_type") == "ephemeral", r.text[:200])
            ck("an empty command never calls the model", not MODEL_CALLS, str(MODEL_CALLS)[:200])
            r = await slash(cx, "ask")
            ck("`/tares ask` with no question -> usage", "ask what?" in r.text, r.text[:200])

            # ── the happy path: ACK inside Slack's 3s, answer via response_url ──
            t0 = time.monotonic()
            r = await slash(cx, "ask what happened to checkout-svc")
            ack_ms = (time.monotonic() - t0) * 1000
            ck("a valid command ACKs 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            ck(f"...well inside Slack's 3s deadline ({ack_ms:.0f}ms)", ack_ms < 3000, f"{ack_ms:.0f}ms")
            ck("...with something for the user to look at while it thinks",
               bool(r.json().get("text")), r.text[:200])

            ck("the answer arrives on response_url", await _until(lambda: bool(REPLIES)),
               "nothing posted to response_url")
            rep = REPLIES[0] if REPLIES else {}
            ck("the answer is posted in-channel", rep.get("response_type") == "in_channel", str(rep)[:200])
            ck("the answer replaces the thinking ACK", rep.get("replace_original") is True, str(rep)[:200])
            ck("the answer carries the model's text", "checkout-svc" in json.dumps(rep), str(rep)[:300])
            ck("the answer is Block Kit", bool(rep.get("blocks")), str(rep)[:200])
            ck("...with a plain-text fallback for the notification", bool(rep.get("text")), str(rep)[:200])
            blocks = json.dumps(rep.get("blocks") or [])
            ck("markdown is converted to Slack mrkdwn (no ** or [](), no stray hashes)",
               "**" not in blocks and "](" not in blocks and "<https://x/y|the timeline>" in blocks,
               blocks[:300])
            ck("the question is echoed with the answer", "checkout-svc" in blocks, blocks[:200])
            ck("a reply outside a thread carries no thread_ts", "thread_ts" not in rep, str(rep)[:200])
            ck("the model was asked exactly once", len(MODEL_CALLS) == 1, str(len(MODEL_CALLS)))
            ck("the question reached the model",
               "checkout-svc" in json.dumps(MODEL_CALLS[0].get("messages", [])),
               str(MODEL_CALLS[:1])[:300])

            # ── the turn is metered: tokens from the stream's usage, priced at write time ──
            um = (await cx.get(f"{B}/api/usage/model")).json()
            ask = um.get("by_surface", {}).get("ask", {})
            ck("the ask turn landed on the usage meter", ask.get("calls") == 1, str(um))
            ck("...with the stream's final usage (input 10, output 20)",
               ask.get("input_tokens") == 10 and ask.get("output_tokens") == 20, str(ask))
            want = (10 * 3.0 + 20 * 15.0) / 1e6
            ck("...costed at the sonnet rate", abs((ask.get("cost_usd") or 0) - want) < 1e-9,
               f"{ask.get('cost_usd')} vs {want}")

            # ── in a thread, the answer belongs in that thread ───────────────
            REPLIES.clear()
            r = await slash(cx, "ask what happened to checkout-svc", thread_ts="1700000000.000100")
            ck("a threaded command ACKs 200", r.status_code == 200, r.text[:200])
            ck("the threaded answer arrives", await _until(lambda: bool(REPLIES)), "no reply")
            rep = REPLIES[0] if REPLIES else {}
            ck("...in the thread it was asked in", rep.get("thread_ts") == "1700000000.000100",
               str(rep)[:200])

            # ── the cost cap (TARES_SLACK_DAILY_CAP=2 for this daemon) ─────
            REPLIES.clear()
            before = len(MODEL_CALLS)
            r = await slash(cx, "ask a third question")
            ck("over the daily cap -> 200 with a readable message",
               r.status_code == 200 and "cap" in r.text.lower(), r.text[:200])
            ck("...ephemeral", r.json().get("response_type") == "ephemeral", r.text[:200])
            await asyncio.sleep(1)
            ck("a capped command never calls the model", len(MODEL_CALLS) == before,
               f"{len(MODEL_CALLS)} vs {before}")
            r = await slash(cx, "ask a question", user="U2")
            ck("the cap is per user, not per workspace", "cap" not in r.text.lower(), r.text[:200])
    finally:
        _stop(proc)
    REPLIES.clear(); MODEL_CALLS.clear()

    # ── phase 3: auth on, no model key — reachable, and it says why it can't answer ──
    env3 = {**base_env, "TARES_SLACK_SIGNING_SECRET": SECRET, "TARES_AUTH_TOKEN": AUTH}
    proc = _spawn(env3)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up (phase 3)", False); return
        async with httpx.AsyncClient(timeout=20) as cx:
            r = await cx.get(f"{B}/api/sources")
            ck("with auth on, an ordinary API call still needs a token", r.status_code == 401,
               str(r.status_code))
            r = await cx.get(f"{B}/api/slack/events")
            ck("the Slack path is public for POST only — GET still needs a token",
               r.status_code == 401, str(r.status_code))

            r = await slash(cx, "ask what happened to checkout-svc")
            ck("a signed slash command is reachable with auth on (no bearer token)",
               r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            ck("no Anthropic key -> a readable message, not silence",
               "Anthropic" in r.text and "warning" in r.text, r.text[:300])
            ck("...and the model was never called", not MODEL_CALLS, str(MODEL_CALLS)[:200])

            body = command_body("ask anything")
            r = await post_raw(cx, body, {"content-type": "application/x-www-form-urlencoded",
                                          "authorization": f"Bearer {AUTH}"})
            ck("a valid bearer token is NOT a substitute for a signature",
               r.status_code == 401, f"{r.status_code} {r.text[:200]}")
    finally:
        _stop(proc)
        stub.shutdown()

    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
