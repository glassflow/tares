"""Slack as a first-class dispatch sink — `slack://channel/<id>` alongside webhooks and agents.

End-to-end against a stub Slack API (TARES_SLACK_API_BASE): configure a bot token, subscribe a
channel to a trigger, ingest until the trigger fires, and assert the channel behaves like any other
subscriber — it appears in the roster, its delivery lands in the ledger and is counted by
`GET /api/agents`, and the message itself is Block Kit with a text fallback and a deep link.

The failure modes are the point of the ticket, so they are asserted directly: `invalid_auth` and
`channel_not_found` come back as HTTP 200 with `{"ok": false}`, and must fail on the FIRST attempt
with that reason in the ledger — not five timeouts. A transient Slack fault must still retry.

The token is a credential: it is write-only over the API, and the environment beats the stored one.

`GET /api/slack/channels` feeds the console's channel picker and is asserted here too. It always
answers 200 and puts the verdict in `reason`, because the console branches on the data rather than
catching exceptions — most importantly on `missing_scope`, which a token issued before the read
scopes were requested returns and which sends the console back to the free-text box.

The endpoint it calls is itself under test: it must be `users.conversations` (channels the bot is
a *member* of, public and private) and not `conversations.list`, which happily lists public
channels the bot was never invited to — subscribing to one of those can only ever fail with
`not_in_channel`, which is the failure the picker exists to prevent.
"""
import asyncio, json, os, signal, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl

import httpx

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

SEED = "/tmp/slack_catalog.yaml"
with open(SEED, "w") as fh:
    fh.write(
        "sources:\n  - name: evt\n    connector: webhook\n    poll: 5s\n"
        "    config:\n      labels:\n        - name: service\n          field: service\n"
        "          primary: true\n"
        # short cooldown: the same entity has to be able to fire again for each failure mode below
        "triggers:\n  - name: incident\n    sources: [evt]\n    key_field: service\n    cooldown: 1s\n"
        "    condition:\n      aggregate: count\n      predicate: '>= 2'\n      window: 1m\n")

DB, PORT, STUB_PORT = "/tmp/slack.duckdb", "8810", "8811"
CHANNEL = "C0123456789"
TOKEN = "xoxb-stored-test-token"
ENV_TOKEN = "xoxb-env-test-token"
PUBLIC_URL = "https://tares.example.com"

# ── stub Slack: chat.postMessage, with the reply switchable per phase ────────
# Slack answers HTTP 200 with {"ok": false, "error": ...} for nearly everything that goes wrong,
# which is exactly why the sink can't read the status code alone.
CALLS: list = []
MODE = "ok"

# users.conversations behind the console's channel picker. Two pages, deliberately out of
# alphabetical order and fat with the fields Slack really sends, so the endpoint has to sort and
# strip. One private channel and two public ones — and `alerts` omits `is_private` entirely, the
# way some Slack payloads do, so the default has to hold. CH_MODE switches the whole call to a
# failure the way MODE does for chat.postMessage.
GETS: list = []
CH_MODE = "ok"
CH_PAGES = {
    "": {"channels": [{"id": "C300", "name": "zeta", "is_channel": True, "is_private": False,
                       "num_members": 4, "purpose": {"value": "x" * 400}},
                      {"id": "C100", "name": "alerts", "is_channel": True, "num_members": 12,
                       "topic": {"value": "incidents"}}],       # no is_private key at all
         "response_metadata": {"next_cursor": "page2"}},
    "page2": {"channels": [{"id": "C200", "name": "ops", "is_group": True, "is_private": True}],
              "response_metadata": {"next_cursor": ""}},
}


class Stub(BaseHTTPRequestHandler):
    def _json(self, out):
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        CALLS.append({"path": self.path, "body": body,
                      "auth": self.headers.get("authorization", "")})
        self._json({"ok": True, "channel": body.get("channel"), "ts": "1700000000.000100"}
                   if MODE == "ok" else {"ok": False, "error": MODE})

    def do_GET(self):
        path, _, query = self.path.partition("?")
        q = dict(parse_qsl(query))
        GETS.append({"path": path, "query": q, "auth": self.headers.get("authorization", "")})
        if CH_MODE.startswith("missing_scope"):
            # "missing_scope:<scope>" lets each phase pick which scope Slack says it wants —
            # with two read scopes in play the detail must echo Slack, not a hardcoded name.
            needed = CH_MODE.partition(":")[2] or "channels:read"
            self._json({"ok": False, "error": "missing_scope", "needed": needed,
                        "provided": "chat:write"})
        elif CH_MODE != "ok":
            self._json({"ok": False, "error": CH_MODE})
        else:
            self._json({"ok": True, **CH_PAGES.get(q.get("cursor", ""), CH_PAGES[""])})

    def log_message(self, *a):
        pass


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
        if await fn():
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


B = f"http://127.0.0.1:{PORT}"


async def _slack_row(cx):
    for a in (await cx.get(f"{B}/api/agents")).json()["agents"]:
        if a["kind"] == "slack":
            return a
    return None


async def _fire(cx, n=3, tag=""):
    """Push enough events to satisfy `count >= 2 over 1m` for one entity."""
    for i in range(n):
        await cx.post(f"{B}/ingest/evt", json={"service": "checkout", "msg": f"500 {tag}#{i}"})


async def main():
    global MODE, CH_MODE
    for p in (DB, DB + ".wal"):
        if os.path.exists(p):
            os.remove(p)

    stub = HTTPServer(("127.0.0.1", int(STUB_PORT)), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    base_env = {**os.environ, "TARES_DB": DB, "TARES_CATALOG": SEED, "TARES_PORT": PORT,
                "TARES_OTLP_GRPC_PORT": "off",
                "TARES_SLACK_API_BASE": f"http://127.0.0.1:{STUB_PORT}",
                "TARES_PUBLIC_URL": PUBLIC_URL,
                "TARES_TRIGGER_DEBOUNCE_SECONDS": "0"}
    base_env.pop("TARES_SLACK_BOT_TOKEN", None)     # phase 1 stores one through the API instead
    proc = _spawn(base_env)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon up", False); return
        async with httpx.AsyncClient(timeout=20) as cx:
            # ── the bot token: write-only, and required before a channel can subscribe ──
            st = (await cx.get(f"{B}/api/settings/slack-bot-token")).json()
            ck("no token configured to start with", st["configured"] is False and st["stored"] is False, str(st))

            # ── the channel picker with no token: 200 and a reason, never an exception ──
            # The console branches on `reason`; a 500 here would blank the whole settings page.
            r = await cx.get(f"{B}/api/slack/channels")
            ch = r.json()
            ck("channels with no token -> 200", r.status_code == 200, r.text)
            ck("channels with no token says reason=no_token",
               ch.get("reason") == "no_token" and ch.get("channels") == [], r.text)

            r = await cx.post(f"{B}/subscribe", json={"trigger": "incident",
                                                      "url": f"slack://channel/{CHANNEL}"})
            ck("subscribing a channel with no token is rejected (400)", r.status_code == 400, r.text)
            ck("...and says why", "token" in r.text.lower(), r.text)

            r = await cx.put(f"{B}/api/settings/slack-bot-token", json={"token": "https://hooks.slack.com/x"})
            ck("a pasted webhook URL is not accepted as a bot token", r.status_code == 400, r.text)

            r = await cx.put(f"{B}/api/settings/slack-bot-token", json={"token": TOKEN})
            ck("PUT token -> 200", r.status_code == 200, r.text)
            ck("PUT never echoes the token", TOKEN not in r.text, r.text)
            st = (await cx.get(f"{B}/api/settings/slack-bot-token")).json()
            ck("token reported configured from the console", st["configured"] and st["source"] == "console", str(st))
            ck("token value is never returned", TOKEN not in json.dumps(st), str(st))
            caps = (await cx.get(f"{B}/api/capabilities")).json()
            ck("capabilities advertises Slack as configured", caps.get("slack_configured") is True, str(caps))

            # ── the channel picker, the happy path ───────────────────────────
            GETS.clear()
            r = await cx.get(f"{B}/api/slack/channels")
            ch = r.json()
            ck("channels -> 200", r.status_code == 200, r.text)
            ck("a good list carries reason=null", ch.get("reason") is None, r.text)
            names = [c["name"] for c in ch.get("channels", [])]
            ck("every page is included — pagination follows next_cursor",
               names == ["alerts", "ops", "zeta"], str(names))
            ck("...which took two calls, the second carrying the cursor",
               len(GETS) == 2 and GETS[0]["query"].get("cursor") is None
               and GETS[1]["query"].get("cursor") == "page2", str(GETS))
            ck("channels are sorted by name, not by Slack's order",
               names == sorted(names), str(names))
            ck("ids come through with the names",
               [c["id"] for c in ch["channels"]] == ["C100", "C200", "C300"], str(ch["channels"]))
            ck("only id, name and is_private are returned — Slack's payload is not passed through",
               all(set(c) == {"id", "name", "is_private"} for c in ch["channels"]),
               str(ch["channels"]))

            # ── public vs private: the console renders a lock instead of a # ──
            priv = {c["name"]: c["is_private"] for c in ch["channels"]}
            ck("a private channel comes back is_private=true", priv.get("ops") is True, str(priv))
            ck("a public channel comes back is_private=false", priv.get("zeta") is False, str(priv))
            ck("a channel with no is_private field defaults to public",
               priv.get("alerts") is False, str(priv))

            ck("the listing is authenticated with the bot token",
               GETS[0]["auth"] == f"Bearer {TOKEN}", GETS[0]["auth"])
            # The endpoint is the whole point of the picker: conversations.list would also answer
            # here, and would offer channels the bot isn't in — every one of which fails its first
            # firing with not_in_channel. users.conversations returns membership only.
            ck("it asks users.conversations, not conversations.list",
               GETS[0]["path"].endswith("/users.conversations")
               and not GETS[0]["path"].endswith("/conversations.list"), str(GETS[0]))
            ck("...for unarchived public AND private channels",
               GETS[0]["query"].get("types") == "public_channel,private_channel"
               and GETS[0]["query"].get("exclude_archived") == "true", str(GETS[0]))
            ck("...on every page, cursor included",
               all(g["path"].endswith("/users.conversations")
                   and g["query"].get("types") == "public_channel,private_channel"
                   for g in GETS), str(GETS))

            # ── the important failure: a token predating the read scopes ──
            # Every existing install has one. The console falls back to the free-text box on this
            # reason and prompts a reconnect, so it must be told apart from a generic error.
            CH_MODE = "missing_scope:channels:read"
            r = await cx.get(f"{B}/api/slack/channels")
            ch = r.json()
            ck("a token without channels:read is still a 200", r.status_code == 200, r.text)
            ck("...with reason=missing_scope and an empty list",
               ch.get("reason") == "missing_scope" and ch.get("channels") == [], r.text)
            ck("...and names the scope it needs", "channels:read" in str(ch.get("detail") or ""),
               str(ch.get("detail")))

            # Two scopes are in play now, so the detail must echo whichever one Slack names — a
            # hardcoded "channels:read" would send the operator to grant the scope they already have.
            CH_MODE = "missing_scope:groups:read"
            ch = (await cx.get(f"{B}/api/slack/channels")).json()
            detail = str(ch.get("detail") or "")
            ck("missing groups:read is reported as groups:read, not channels:read",
               ch.get("reason") == "missing_scope" and "groups:read" in detail
               and "channels:read" not in detail, detail)

            CH_MODE = "invalid_auth"
            ch = (await cx.get(f"{B}/api/slack/channels")).json()
            ck("any other Slack failure is reason=error with a readable detail",
               ch.get("reason") == "error" and "invalid_auth" in str(ch.get("detail") or "")
               and ch.get("channels") == [], str(ch))
            CH_MODE = "ok"

            # ── subscribing a channel ────────────────────────────────────────
            r = await cx.post(f"{B}/subscribe", json={"trigger": "incident", "url": "slack://channel/not a channel!"})
            ck("a malformed channel is rejected (400)", r.status_code == 400, r.text)
            r = await cx.post(f"{B}/subscribe", json={"trigger": "nope", "url": f"slack://channel/{CHANNEL}"})
            ck("an unknown trigger is still a 404", r.status_code == 404, str(r.status_code))

            r = await cx.post(f"{B}/subscribe", json={"trigger": "incident",
                                                      "url": f"slack://channel/{CHANNEL}"})
            ck("subscribe a channel -> 200", r.status_code == 200, r.text)

            row = await _slack_row(cx)
            ck("the channel is in the roster", row is not None, str(row))
            ck("roster tags it kind=slack", row and row["kind"] == "slack", str(row))
            ck("roster names it by channel, not by raw URL",
               row and row["name"] == f"#{CHANNEL}" and "slack://" not in row["endpoint"], str(row))
            ck("roster shows it woken by the trigger", row and "incident" in row["triggers"], str(row))

            # ── it delivers ──────────────────────────────────────────────────
            CALLS.clear()
            await _fire(cx, tag="ok ")
            ck("a firing posts to Slack", await _until(lambda: asyncio.sleep(0, result=bool(CALLS))),
               "no chat.postMessage received")
            call = CALLS[0] if CALLS else {"body": {}, "auth": "", "path": ""}
            ck("posted to chat.postMessage", call["path"].endswith("/chat.postMessage"), call["path"])
            ck("authenticated with the bot token", call["auth"] == f"Bearer {TOKEN}", call["auth"])
            ck("addressed the subscribed channel", call["body"].get("channel") == CHANNEL, str(call["body"])[:200])
            blocks = json.dumps(call["body"].get("blocks") or [])
            ck("message is Block Kit", bool(call["body"].get("blocks")), str(call["body"])[:200])
            ck("message has a plain-text fallback for notifications",
               "incident" in str(call["body"].get("text", "")), str(call["body"].get("text")))
            ck("blocks name the trigger and the entity",
               "incident" in blocks and "checkout" in blocks, blocks[:300])
            # The alert links the FIRING, not the entity: "what fired and what did it carry" is
            # the question someone reading it in Slack has. Agent findings still link the entity.
            ck("blocks link the dispatch when TARES_PUBLIC_URL is set",
               f"{PUBLIC_URL}/dispatches/" in blocks, blocks[:400])
            ck("the alert does not link the entity instead",
               f"{PUBLIC_URL}/explore?key=" not in blocks, blocks[:400])
            ck("the posted message does not unfurl",
               call["body"].get("unfurl_links") is False, str(call["body"])[:200])

            async def _counted():
                a = await _slack_row(cx)
                return bool(a) and a.get("delivered_ok_24h", 0) >= 1
            ck("GET /api/agents counts the delivery", await _until(_counted), str(await _slack_row(cx)))

            disp = (await cx.get(f"{B}/api/activity/dispatches")).json()[0]
            detail = (await cx.get(f"{B}/api/activity/dispatches/{disp['dispatch_id']}")).json()
            dv = next((d for d in detail.get("deliveries", []) if d["agent"] == f"#{CHANNEL}"), None)
            ck("the firing's ledger lists the channel as delivered", dv is not None and dv["ok"], str(detail)[:300])

            # ── a revoked token is DEFINITIVE: one attempt, readable reason ──
            for mode, needle in (("invalid_auth", "invalid_auth"), ("channel_not_found", "channel_not_found")):
                before = (await _slack_row(cx)).get("delivered_fail_total", 0)
                MODE = mode
                CALLS.clear()
                await asyncio.sleep(1.2)          # clear the trigger's per-entity cooldown
                await _fire(cx, tag=mode + " ")

                async def _failed():
                    a = await _slack_row(cx)
                    return bool(a) and a.get("delivered_fail_total", 0) > before
                ck(f"{mode}: the delivery is recorded as failed", await _until(_failed), str(await _slack_row(cx)))
                a = await _slack_row(cx)
                ck(f"{mode}: the ledger carries the reason", needle in str(a.get("last_error") or ""),
                   str(a.get("last_error")))
                ck(f"{mode}: the endpoint reads as failing", a.get("unhealthy") is True, str(a))
                # the whole point: 5 attempts of backoff would take ~15s and bury the reason
                ck(f"{mode}: tried exactly once, not five times", len(CALLS) == 1, f"{len(CALLS)} attempts")

            # ── a transient Slack fault DOES retry ───────────────────────────
            MODE = "internal_error"
            CALLS.clear()
            await asyncio.sleep(1.2)
            await _fire(cx, tag="transient ")
            ck("a transient Slack error is retried", await _until(lambda: asyncio.sleep(0, result=len(CALLS) >= 2), tries=20),
               f"{len(CALLS)} attempts")
            MODE = "ok"
    finally:
        _stop(proc)

    # ── the environment beats the stored token ──────────────────────────────
    # An operator's deployment config must never be silently overridden by something typed into the
    # console months earlier — the same rule the Anthropic key follows.
    env2 = {**base_env, "TARES_SLACK_BOT_TOKEN": ENV_TOKEN}
    proc = _spawn(env2)
    try:
        if not await _wait(f"{B}/health"):
            ck("daemon back up", False)
        else:
            async with httpx.AsyncClient(timeout=20) as cx:
                st = (await cx.get(f"{B}/api/settings/slack-bot-token")).json()
                ck("env token wins over the stored one",
                   st["source"] == "env:TARES_SLACK_BOT_TOKEN" and st["env_overrides"] is True, str(st))
                ck("the console is told a stored token exists but is unused", st["stored"] is True, str(st))
                ck("neither token is ever returned",
                   TOKEN not in json.dumps(st) and ENV_TOKEN not in json.dumps(st), str(st))

                CALLS.clear()
                await _fire(cx, tag="env ")
                ck("deliveries use the env token",
                   await _until(lambda: asyncio.sleep(0, result=bool(CALLS)), tries=30)
                   and CALLS[0]["auth"] == f"Bearer {ENV_TOKEN}",
                   str(CALLS[:1])[:200])

                r = await cx.delete(f"{B}/api/settings/slack-bot-token")
                ck("DELETE clears the stored token", r.status_code == 200, r.text)
                st = (await cx.get(f"{B}/api/settings/slack-bot-token")).json()
                ck("still configured afterwards — the env token remains",
                   st["configured"] is True and st["stored"] is False, str(st))
    finally:
        _stop(proc)
        stub.shutdown()

    # ── NF-126: the message a human actually reads ────────────────────────────
    import os as _os
    from tares import slack as _s

    _os.environ["TARES_PUBLIC_URL"] = "https://cell.example.com"
    m = _s.build_message("t1", "svc-a", "[T-1734s] something happened",
                         "2026-08-07T11:47:17.162746+00:00", "ab41d2bbfeec4319bf288abe607fa8ab")
    ck("unfurling is off — a host label must not make Slack fetch the customer's site",
       m.get("unfurl_links") is False and m.get("unfurl_media") is False, str(m)[:200])
    blocks = {b["type"]: b for b in m["blocks"]}
    ck("a firing reads like a Rius alert: the trigger as header, then fields",
       blocks["header"]["text"]["text"] == "t1"
       and any("*Entity*\nsvc-a" == f["text"] for f in m["blocks"][1]["fields"]), str(m["blocks"][:2]))
    fired = next(f["text"] for f in m["blocks"][1]["fields"] if f["text"].startswith("*Fired*"))
    ck("when it fired uses Slack's date token, not a raw ISO string",
       "<!date^" in fired and "{time}" in fired, fired)
    btn = blocks["actions"]["elements"][0]
    ck("a button opens the DISPATCH, not the entity",
       btn["url"].endswith("/dispatches/ab41d2bbfeec4319bf288abe607fa8ab")
       and btn["text"]["text"] == "View firing in Tares", str(btn))
    ck("event ages are readable in the Slack copy", "[29m ago]" in m["blocks"][2]["text"]["text"],
       m["blocks"][2]["text"]["text"])

    _os.environ["TARES_PUBLIC_URL"] = ""
    ck("no button when the instance has no reachable address",
       not any(b["type"] == "actions" for b in _s.build_message("t1", "k", "p", None, "abc")["blocks"]))
    ck("a firing with no dispatch id still builds",
       "blocks" in _s.build_message("t1", "k", "p"))

    # ── TR-275: an agent's finding, scannable in the channel ─────────────────
    _os.environ["TARES_PUBLIC_URL"] = "https://cell.example.com"
    note = ("I have sufficient evidence to diagnose this.\n\n## 1. What is failing\n\n"
            "ingress-nginx is returning 404s to one scanner.\n\n"
            "| Priority | Action |\n|---|---|\n"
            "| Immediate | No service action required; the upstream correctly returns 404 and nothing is exposed. |\n"
            "| Short-term | Block 35.221.224.65 at the ingress level; it is an active scanner. |\n\n"
            "**Summary:** A security scanner, not a service fault. Block the IP.")
    fm = _s.build_finding_message("rca", "escalate", "ingress-nginx", note, verdict="resolved",
                                  model="claude-sonnet-5", run_id="run_1",
                                  when="2026-09-28T20:46:00+00:00")
    kinds = [b["type"] for b in fm["blocks"]]
    ck("finding: header, fields, excerpt, buttons, context",
       kinds == ["header", "section", "section", "actions", "context"], str(kinds))
    ck("the entity is the header", fm["blocks"][0]["text"]["text"] == "ingress-nginx")
    labels = [f["text"].split("\n")[0] for f in fm["blocks"][1]["fields"]]
    ck("fields: agent, trigger, verdict, model, when",
       labels == ["*Agent*", "*Trigger*", "*Verdict*", "*Model*", "*When*"], str(labels))
    ck("the excerpt is the note's own summary",
       fm["blocks"][2]["text"]["text"].startswith("*Summary:* A security scanner"),
       fm["blocks"][2]["text"]["text"])
    urls = [e["url"] for e in fm["blocks"][3]["elements"]]
    ck("buttons open the run and the entity's timeline",
       urls == ["https://cell.example.com/agents/rca?tab=runs&run=run_1",
                "https://cell.example.com/explore?key=ingress-nginx"], str(urls))
    ck("a note that fits in one block is posted whole, no thread", not _s.needs_thread(note)
       and not _s.needs_thread("Traffic is normal."))
    ck("a note longer than one block goes in the thread", _s.needs_thread(note * 8))
    th = "\n".join(b["text"]["text"] for b in _s.build_finding_thread(note))
    ck("a wide table becomes one line per row, not a code block",
       "•  *Immediate*: No service action required" in th and "```" not in th, th)
    _os.environ["TARES_PUBLIC_URL"] = ""
    bare = _s.build_finding_message("rca", "escalate", "k", "short", full_note=True)
    ck("no buttons without a public address; the note still renders",
       "actions" not in [b["type"] for b in bare["blocks"]]
       and any(b.get("text", {}).get("text") == "short" for b in bare["blocks"]), str(bare["blocks"]))
    long_code = "intro\n\n```\n" + "\n".join(f"line {i} " + "x" * 60 for i in range(90)) + "\n```\n\nafter"
    parts = [b["text"]["text"] for b in _s._sections(long_code)]
    ck("a code block longer than a section is split with every part fenced",
       len(parts) >= 2 and all(p.count("```") % 2 == 0 for p in parts)
       and all(len(p) <= 3000 for p in parts), str([p.count("```") for p in parts]))
    ck("an excerpt is cut at a sentence",
       _s.excerpt("One. " * 200, 50).endswith("One."), _s.excerpt("One. " * 200, 50))

    md = ("You have **3 sources**:\n\n"
          "| # | Name | Status |\n|---|------|--------|\n"
          "| 1 | `docs-github` | ok |\n| 2 | `rius-prod` | push |\n\n"
          "---\n\n- first\n- second\n")
    out = _s.to_mrkdwn(md)
    ck("a markdown table becomes a monospace block, not raw pipes",
       "```" in out and "|---|" not in out and "| 1 |" not in out, out)
    ck("table columns are aligned", "docs-github  ok" in out.replace("   ", "  "), out)
    ck("backticks inside a cell are stripped — a code block renders them literally",
       "`docs-github`" not in out, out)
    ck("a horizontal rule does not survive as three dashes",
       not any(l.strip() == "---" for l in out.splitlines()), out)
    ck("bullets render as bullets", "•  first" in out, out)
    ck("bold still converts", "*3 sources*" in out and "**" not in out, out)
    ck("a lone pipe line is prose, not a table",
       "```" not in _s.to_mrkdwn("use a | b to split"), _s.to_mrkdwn("use a | b to split"))
    a = _s.build_answer("q", "answer")
    ck("answers do not unfurl either",
       a.get("unfurl_links") is False and a.get("unfurl_media") is False, str(a)[:160])

    print(f"\n{P} passed, {F} failed")
    raise SystemExit(1 if F else 0)


asyncio.run(main())
