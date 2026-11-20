"""A direct message on Slack when something new waits for a person (TR-1458).

The reader reads each person's "Waiting for you" (factory_api.waiting_for_person) and sends one
message per new item: its ask, what it holds up, and a link to Home at that item. Slack's
lookup by email and chat.postMessage are stubbed. Checked here:

  - no Slack connected: nothing is read or sent;
  - the first read only records what already waits; a new item then goes once, to the person it
    waits for and nobody else, and never twice;
  - a person who switched it off (PUT /api/me/slack, their own key) gets nothing, and the items
    that came while it was off are not sent when they switch it back on;
  - after more than an hour away, what waits is recorded, not sent; so is what already waits for
    someone who joins the cell later;
  - more than three new items in one read: one message that lists them (TR-1460);
  - an item answered anywhere edits its message ("Answered by you from the feed", "by Alex on
    Home"), sends nothing new, and is not edited twice; in a list, its line is struck through;
  - an item that goes with no answer recorded says it no longer waits; an edit Slack refuses for
    a while is tried again;
  - a restart (a new reader on the same store) sends nothing again and still edits what it sent;
  - a message Slack refuses for good is shown in the person's Settings until one goes, in one
    plain sentence made from Slack's real error (slack.classify), never its code (TR-1511);
  - switching messages off, a new bot token, or disconnecting Slack clears that notice;
  - a refusal of the app itself (a revoked token, no chat:write) is shown to everyone, also to
    someone who had nothing new, until Slack takes the token again;
  - an email Slack does not know, and a bot without users:read.email, send nothing and say so.

Run: .venv/bin/python tests/test_waiting_slack.py
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _env import scrub_os_environ
scrub_os_environ()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-waiting-slack-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-waiting-slack"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
os.environ["TARES_PUBLIC_URL"] = "https://cell.example.com"

import httpx

from tares import slack as slack_mod
from tares import waiting_slack as ws
from tares import words
from tares.store import now_utc

P = F = 0


def ck(label, cond, detail: object = ""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


class FakeSlack:
    def __init__(self):
        self.sent: list[dict] = []
        self.updates: list[dict] = []
        self.update_error: tuple[str, bool] | None = None
        self.n = 0
        self.users = {"alex@example.com": "U_ALEX", "kim@example.com": "U_KIM"}
        self.lookups = 0
        self.error: str | None = None
        self.send_error: tuple[str, bool] | None = None
        self.refused: list[dict] = []

    def refuse(self, code: str | None):
        """Refuse every send from now on as Slack does with `code`, through the real classify, so
        the error text is what the reader gets from Slack (hint included)."""
        if code is None:
            self.send_error = None
            return
        ok, err, retry = slack_mod.classify(200, {"ok": False, "error": code})
        assert not ok
        self.send_error = (err, retry)

    async def send(self, token, msg):
        if self.send_error:
            self.refused.append(msg)
            return None, self.send_error[0], self.send_error[1]
        self.sent.append(msg)
        self.n += 1
        msg["ts"] = f"{self.n}.0001"
        return {"channel": "D" + msg["channel"], "ts": msg["ts"]}, None, False

    async def update(self, token, msg):
        if self.update_error:
            return False, self.update_error[0], self.update_error[1]
        self.updates.append(msg)
        return True, None, False

    def edits(self) -> list[dict]:
        out, self.updates = self.updates, []
        return out

    async def lookup(self, token, email):
        self.lookups += 1
        if self.error:
            return None, self.error
        return self.users.get(email), None

    def take(self) -> list[dict]:
        out, self.sent = self.sent, []
        return out


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    slack = FakeSlack()
    clock = {"now": now_utc()}
    # the daemon's own reader (its routes read it), with Slack and the clock stubbed
    reader = app.state.waiting_slack
    reader._send, reader._update, reader._lookup = slack.send, slack.update, slack.lookup
    reader.clock = lambda: clock["now"]

    alex = store.create_person("Alex", email="alex@example.com", is_admin=True)
    kim = store.create_person("Kim", email="kim@example.com")
    secret = "kim-key-waiting-slack"
    alex_key = "alex-key-waiting-slack"
    store.insert_api_key("k_alex", "Alex's key", "nvf_alex", hashlib.sha256(alex_key.encode()).hexdigest(),
                         ["read", "ingest"], person_id=alex["id"])
    store.insert_api_key("k_kim", "Kim's key", "nvf_kim", hashlib.sha256(secret.encode()).hexdigest(),
                         ["read", "ingest"], person_id=kim["id"])
    uid = "uc_shop"
    store.create_project(uid, "custom", "shop", {}, goal="Sell things")
    store.set_project_kind(uid, "software_factory")

    def ask(question, to=None, blocks=""):
        return store.add_desk_item(uid, question, "", ["Yes", "No"], "Yes", "", blocks, [], True,
                                   "crew-builder-1", [], now_utc(), to_person_id=to)

    async def read(minutes=1, r=None):
        clock["now"] = clock["now"] + timedelta(minutes=minutes)
        return await (r or reader).read_once()

    async def reply(d, words, key=secret, via="feed"):
        async with cx(key) as c:
            r = await c.post(f"/api/desk/{d['label']}/reply", json={"words": words, "choice": words},
                             headers={"X-Tares-Via": via})
            assert r.status_code == 200, r.text

    def cx(key=TOKEN):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                 headers={"Authorization": f"Bearer {key}"})

    print("== no Slack connected: nothing is read or sent ==")
    ask("Use Postgres for the cart?", to=kim["id"])
    got = await read()
    ck("skipped, nothing sent", got.get("skipped") and not slack.sent, (got, slack.sent))
    ck("nothing recorded either", store.told_items(kim["id"]) == set(), store.told_items(kim["id"]))

    os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-test"
    print("== the first read only records what already waits ==")
    await read()
    ck("nothing sent for what waited before", slack.take() == [])
    ck("Kim's list was read once", ws.SEEN in store.told_items(kim["id"]))

    print("== a new item goes once, to the person it waits for ==")
    d = ask("Ship the cart this week?", to=kim["id"], blocks="M1: cart")
    await read()
    sent = slack.take()
    ck("exactly one message", len(sent) == 1, sent)
    m = sent[0] if sent else {}
    ck("to Kim's Slack user, not Alex's", m.get("channel") == "U_KIM", m.get("channel"))
    ck("its text is the ask", m.get("text") == "Waiting for you: Ship the cart this week?", m.get("text"))
    body = str(m.get("blocks"))
    ck("it says what it holds up", "Holds up M1: cart." in body, body)
    ck("it names the project", "Waiting for you in shop" in body, body)
    item = next(i for i in reader.waiting_for(kim, now_utc()) if d["question"] in str(i.get("detail")))
    link = f"https://cell.example.com/?item={item['id'].replace(':', '%3A')}"
    ck("it links to Home on that item", link in body, (link, body))
    ck("its button reads Open in Tares", "Open in Tares" in body and "open_item" in body, body)
    await read()
    ck("never twice", slack.take() == [])

    print("== an ask Slack would read as markup is sent as plain words ==")
    ask("Ping <!here> and open <https://evil.example|the feed>?", to=kim["id"])
    await read()
    m = (slack.take() or [{}])[0]
    raw = [m.get("text", "")] + [str(b) for b in m.get("blocks", [])]
    ck("no mention and no link of its own, in the text or the blocks",
       not any("<!here>" in x or "<https://evil" in x for x in raw) and "&lt;!here&gt;" in m.get("text", ""), raw)

    print("== Kim switches it off with her own key ==")
    async with cx(secret) as c:
        r = await c.get("/api/me/slack")
        ck("on until she switches it off, Slack connected, found", r.status_code == 200 and r.json()["on"] is True
           and r.json()["connected"] is True and r.json()["found"] is True, r.text)
        r = await c.put("/api/me/slack", json={"on": False})
        ck("switched off", r.status_code == 200 and r.json()["on"] is False, r.text)
        r = await c.put("/api/me/slack", json={"on": "no"})
        ck("on must be true or false", r.status_code == 400, r.text)
    async with cx() as c:
        r = await c.get("/api/me/slack")
        ck("the root token is no person", r.status_code == 400, r.text)
    ck("the unclaimed local of a cell of one is never messaged, so Settings offers no switch",
       reader.status_for(store.get_person("local"))["claimed"] is False
       and reader.status_for(kim)["claimed"] is True)
    ask("Rename the cart?", to=kim["id"])
    await read()
    ck("off: nothing sent", slack.take() == [])
    async with cx(secret) as c:
        await c.put("/api/me/slack", json={"on": True})
    await read()
    ck("back on: what came while off is not sent", slack.take() == [])
    ask("Add a wishlist?", to=kim["id"])
    await read()
    ck("back on: the next new one is", [m["channel"] for m in slack.take()] == ["U_KIM"])

    print("== away for more than an hour: recorded, not sent ==")
    ask("Drop the old checkout?", to=kim["id"])
    await read(minutes=90)
    ck("nothing sent after the gap", slack.take() == [])
    ask("Keep the coupon field?", to=kim["id"])
    await read()
    ck("the reader is back: the next new one goes", len(slack.take()) == 1)

    print("== three new at once: three messages ==")
    three = [ask(f"Small {n}?", to=kim["id"]) for n in range(3)]
    await read()
    ck("one message each", len(slack.take()) == 3)

    print("== more than three new at once: one message that lists them ==")
    burst = [ask(f"Question {n}?", to=kim["id"]) for n in range(12)]
    await read()
    sent = slack.take()
    ck("one message", len(sent) == 1, [m["text"] for m in sent])
    lst = sent[0] if sent else {}
    ck("it says how many wait", lst.get("text") == "12 things wait for you", lst.get("text"))
    body = str(lst.get("blocks"))
    ck("it lists the first ten and says how many more",
       "• Question 0?" in body and "• Question 9?" in body and "Question 10?" not in body
       and "and 2 more" in body, body)
    ck("the list links to Home", "'url': 'https://cell.example.com/'" in body and "?item=" not in body, body)
    await read()
    ck("and none of them again", slack.take() == [])

    print("== answered from the feed: the message is edited, nothing new is sent ==")
    await reply(three[0], "Yes")
    await read()
    ck("nothing new sent", slack.take() == [])
    ed = slack.edits()
    ck("one edit", len(ed) == 1, ed)
    e = ed[0] if ed else {}
    ck("of that item's own message, in Kim's DM", e.get("channel") == "DU_KIM" and e.get("ts"), e)
    ck("it says who answered it and where", "Answered by you from the feed." in str(e.get("blocks")), e)
    ck("and no longer offers the button", "open_item" not in str(e.get("blocks")), e)
    await read()
    ck("not edited twice", slack.edits() == [])

    print("== answered by someone else, on Home ==")
    store.add_answer(f"question:{three[1]['label']}", "question", uid, "Small 1?", None, "No", "console",
                     alex["id"], now_utc())
    store.close_desk_item(three[1]["id"], "answered", None, now_utc())
    await read()
    ed = slack.edits()
    ck("Answered by Alex on Home", len(ed) == 1 and "Answered by Alex on Home." in str(ed[0]["blocks"]), ed)

    from tares import answers as answers_mod
    got = store.last_answer(answers_mod.item_ids(f"desk:{three[1]['label']}"))
    ck("a team cell's desk: id finds the answer recorded as question:",
       got and got["said"] == "No" and got["person"]["name"] == "Alex", got)

    print("== gone with no answer recorded ==")
    async with cx() as c:
        r = await c.post(f"/api/desk/{three[2]['label']}/withdraw", json={"reason": "not needed"})
        ck("withdrawn", r.status_code == 200, r.text)
    await read()
    ed = slack.edits()
    ck("it says it no longer waits", len(ed) == 1 and ws.GONE in str(ed[0]["blocks"]), ed)

    print("== one item of a list answered: its line is struck through ==")
    await reply(burst[0], "Yes")
    await reply(burst[1], "No")
    await read()
    ck("nothing new sent", slack.take() == [])
    ed = slack.edits()
    ck("one edit for the two", len(ed) == 1, len(ed))
    e = ed[0] if ed else {}
    body = str(e.get("blocks"))
    ck("of the list's message", e.get("ts") == lst.get("ts"), (e.get("ts"), lst.get("ts")))
    ck("the two struck through, saying who", "~Question 0?~ Answered by you from the feed." in body
       and "~Question 1?~ Answered by you from the feed." in body, body)
    ck("the others as they were, and the count is what still waits",
       "• Question 2?" in body and e.get("text") == "10 things wait for you", (e.get("text"), body))

    print("== a restart: nothing sent again, and what it sent is still edited ==")
    again = ws.WaitingSlack(store, reader.waiting_for, send=slack.send, update=slack.update,
                            lookup=slack.lookup, clock=lambda: clock["now"])
    await read(r=again)
    ck("nothing sent again", slack.take() == [])
    await reply(burst[2], "Yes")
    await read(r=again)
    ed = slack.edits()
    ck("the new reader edits the old message",
       len(ed) == 1 and ed[0]["ts"] == lst.get("ts") and "~Question 2?~" in str(ed[0]["blocks"]), ed)
    ck("and keeps the earlier lines", ed and "~Question 0?~" in str(ed[0]["blocks"]))

    print("== an edit Slack refuses for a while is tried again ==")
    await reply(burst[3], "Yes")
    slack.update_error = ("slack: ratelimited", True)
    await read()
    ck("refused: nothing recorded", slack.edits() == [])
    slack.update_error = None
    await read()
    ed = slack.edits()
    ck("the next read edits it", len(ed) == 1 and "~Question 3?~" in str(ed[0]["blocks"]), ed)
    await reply(burst[4], "Yes")
    slack.update_error = ("slack: message_not_found", False)
    await read()
    slack.update_error = None
    await read()
    ck("a refusal that will not change is not tried every minute", slack.edits() == [])

    print("== a message Slack refuses for a while is sent on the next read ==")
    busy = ask("Ship it while Slack is busy?", to=alex["id"])
    slack.send_error = ("slack: ratelimited", True)
    await read()
    ck("refused: it was tried, nothing went", len(slack.refused) == 1 and slack.take() == [], slack.refused)
    slack.send_error = None
    await read()
    got = slack.take()
    ck("the next read sends it", len(got) == 1 and "Ship it while Slack is busy?" in str(got), got)
    await read()
    ck("and only once", slack.take() == [])
    gone = ask("Ship it to a closed channel?", to=alex["id"])
    slack.refused = []
    slack.refuse("channel_not_found")
    await read()
    slack.refuse(None)
    await read()
    ck("a refusal that will not change is not tried every minute",
       len(slack.refused) == 1 and slack.take() == [], slack.refused)

    async def notice(key):
        async with cx(key) as c:
            r = await c.get("/api/me/slack")
        assert r.status_code == 200, r.text
        return r.json()["problem"]

    closed = ws.PERSON_REFUSED["channel_not_found"]
    said = await notice(alex_key)
    ck("Settings says, in words, why the closed DM got nothing", said == closed, said)
    ck("no Slack code, no brackets", said and "_" not in said and "(" not in said and "slack:" not in said, said)
    ck("only to the person it was for", reader.status_for(kim)["problem"] is None, reader.status_for(kim))
    opened = ask("Ship it to an open channel?", to=alex["id"])
    await read()
    ck("the next message that goes clears it",
       len(slack.take()) == 1 and reader.status_for(alex)["problem"] is None, reader.status_for(alex))
    for d in (busy, gone, opened):      # off Alex's list again, for the checks below
        store.close_desk_item(d["id"], "dismissed", None, now_utc())
    await read()
    slack.edits()

    async def refused_for_alex(code, question):
        slack.refuse(code)
        d = ask(question, to=alex["id"])
        await read()
        slack.refuse(None)
        store.close_desk_item(d["id"], "dismissed", None, now_utc())
        return await notice(alex_key)

    print("== switching messages off clears the notice ==")
    ck("an archived DM", await refused_for_alex("is_archived", "Ship to the archive?") == closed)
    async with cx(alex_key) as c:
        off = (await c.put("/api/me/slack", json={"on": False})).json()
        on = (await c.put("/api/me/slack", json={"on": True})).json()
    ck("off: no notice", off["problem"] is None, off)
    ck("on again: the old one does not come back", on["problem"] is None, on)

    print("== a new bot token, or Slack disconnected, clears the notice ==")
    ck("refused again", await refused_for_alex("channel_not_found", "Ship to nowhere?") == closed)
    os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-test-2"
    ck("a new token: no notice", await notice(alex_key) is None)
    ck("refused under it", await refused_for_alex("channel_not_found", "Ship to nowhere again?") == closed)
    del os.environ["TARES_SLACK_BOT_TOKEN"]
    ck("disconnected: not connected", reader.status_for(alex)["connected"] is False)
    os.environ["TARES_SLACK_BOT_TOKEN"] = "xoxb-test-2"
    ck("connected again with the same token: the old notice does not come back",
       await notice(alex_key) is None)

    print("== a message Slack finds malformed: told it did not go, no code ==")
    said = await refused_for_alex("invalid_blocks", "Ship a bad message?")
    ck("the person reads that it did not go", said == ws.OTHER_REFUSED, said)
    good = ask("Ship a good one?", to=alex["id"])
    await read()
    ck("cleared by the next message that goes", len(slack.take()) == 1 and await notice(alex_key) is None)

    print("== a revoked token is everyone's problem ==")
    revoked = ws.APP_REFUSED["token_revoked"]
    said = await refused_for_alex("token_revoked", "Ship with a revoked token?")
    ck("Alex, whose message met it, reads it", said == revoked, said)
    ck("Kim, who had nothing new, reads it too", await notice(secret) == revoked)
    ck("it is not recorded as Alex's own", alex["id"] not in reader.send_failed, reader.send_failed)
    good2 = ask("Ship it now the token works?", to=alex["id"])
    await read()
    ck("Slack takes the token again: the message goes, nobody's Settings names it",
       len(slack.take()) == 1 and await notice(secret) is None and await notice(alex_key) is None)

    print("== a lookup refused the same way, before any send: everyone's problem too ==")
    reader._users.clear()
    slack.error = "token_revoked"
    logo = ask("Pick a logo?", to=kim["id"])
    await read()
    ck("nothing went", slack.take() == [])
    ck("Alex reads it, though the lookup was for Kim", await notice(alex_key) == revoked)
    slack.error = None
    slack.refuse("channel_not_found")
    await read()
    slack.refuse(None)
    ck("a lookup Slack answers clears it, whatever then happens to the message",
       await notice(alex_key) is None)
    ck("Kim reads only her own", await notice(secret) == closed)
    tidy = ask("Pick a font?", to=kim["id"])
    await read()
    ck("her next message goes and clears hers", [m["channel"] for m in slack.take()] == ["U_KIM"]
       and await notice(secret) is None)
    ck("no chat:write scope: everyone's too",
       await refused_for_alex("missing_scope", "Ship without the scope?") == ws.APP_REFUSED["missing_scope"]
       and await notice(secret) == ws.APP_REFUSED["missing_scope"])
    ck("its words name the chat:write scope", "chat:write" in str(await notice(secret)))
    back = ask("Ship once it is back?", to=alex["id"])
    await read()
    ck("cleared when a message goes", len(slack.take()) == 1 and await notice(secret) is None)

    print("== the words of every notice ==")
    texts = {ws.NO_SCOPE, ws.OTHER_REFUSED, *ws.APP_REFUSED.values(), *ws.PERSON_REFUSED.values()}
    bad = [(t, words.offenders(t)) for t in texts if words.offenders(t)]
    ck("every notice passes the words check", not bad, bad)
    ck("none ends in a full stop (the console adds it)", not [t for t in texts if t.endswith(".")])
    ck("each code Slack names in the reader maps to its own words",
       all(ws.slack_code(slack_mod.classify(200, {"ok": False, "error": c})[1]) == c
           for c in [*ws.APP_REFUSED, *ws.PERSON_REFUSED]))
    for d in (good, good2, logo, tidy, back):        # off the lists again, for the checks below
        store.close_desk_item(d["id"], "dismissed", None, now_utc())
    await read()
    slack.take()
    slack.edits()

    print("== an email Slack does not know ==")
    del slack.users["kim@example.com"]
    reader._users.clear()
    ask("Split the cart service?", to=kim["id"])
    await read()
    ck("nothing sent", slack.take() == [])
    async with cx(secret) as c:
        r = await c.get("/api/me/slack")
        ck("Settings says Slack does not know her", r.json()["found"] is False, r.text)
    slack.users["kim@example.com"] = "U_KIM"
    reader._users.clear()
    ask("Cache the prices?", to=kim["id"])
    await read()
    ck("found again: the next one goes, the missed one does not", [m["text"] for m in slack.take()]
       == ["Waiting for you: Cache the prices?"])

    print("== a bot that cannot look people up by email ==")
    reader._users.clear()
    slack.error = "missing_scope"
    ask("Move to a queue?", to=kim["id"])
    await read()
    ck("nothing sent", slack.take() == [])
    async def notice():
        async with cx(secret) as c:
            return (await c.get("/api/me/slack")).json()["problem"]
    ck("Settings names the missing scope", "users:read.email" in str(await notice()))
    await read()
    ck("a read with nothing new keeps it (TR-1468)", "users:read.email" in str(await notice()))
    slack.error = "ratelimited"
    ask("Add search?", to=kim["id"])
    await read()
    ck("Slack could not say: nothing sent yet", slack.take() == [])
    slack.error = None
    await read()
    ck("the next read sends what waited for the lookup", [m["text"] for m in slack.take()]
       == ["Waiting for you: Add search?"])
    ck("and Settings no longer names the scope", await notice() is None)

    print("== the scope added while nothing new waits: the notice goes on the next read ==")
    reader._users.clear()
    slack.error = "missing_scope"
    ask("Rename the queue?", to=kim["id"])
    await read()
    ck("the scope is missing", "users:read.email" in str(await notice()))
    slack.error = None
    looked = slack.lookups
    await read()
    ck("one lookup asks whether it is there now", slack.lookups == looked + 1, slack.lookups - looked)
    ck("it is, and Settings says nothing", await notice() is None)
    await read()
    ck("no more asking once it is there", slack.lookups == looked + 1, slack.lookups - looked)
    ck("what came while it was missing is not sent late", slack.take() == [])

    print("== someone who joins later: what already waits for them is not sent ==")
    lee = store.create_person("Lee", email="lee@example.com")
    slack.users["lee@example.com"] = "U_LEE"
    ask("Pick the payment provider?", to=lee["id"])
    await read()
    ck("Lee's first read sends nothing", slack.take() == [])
    ask("Add Apple Pay?", to=lee["id"])
    await read()
    ck("Lee's next new one goes", [m["channel"] for m in slack.take()] == ["U_LEE"])

    print("== a private project Kim is not on: nothing of it reaches her (TR-1468) ==")
    vault = "uc_vault"
    store.create_project(vault, "custom", "Vault", {}, goal="Keep the codes")
    store.set_project_kind(vault, "software_factory")
    store.set_project_scope(vault, "private")
    codes = store.add_desk_item(vault, "Rotate the vault codes?", "", ["Yes", "No"], "Yes", "", "", [], True,
                                "crew-builder-1", [], now_utc(), to_person_id=kim["id"])
    await read()
    ck("not in Kim's list", not [i for i in reader.waiting_for(kim, now_utc())
                                 if "vault" in json.dumps(i).lower()])
    ck("no message about it", not [m for m in slack.take() if "vault" in json.dumps(m).lower()])
    store.add_project_member(vault, kim["id"])
    await read()
    ck("once she is on it, it goes to her", [m["text"] for m in slack.take()]
       == ["Waiting for you: Rotate the vault codes?"])
    store.close_desk_item(codes["id"], "dismissed", None, now_utc())
    await read()
    slack.edits()

    print("== Alex was never told about Kim's questions ==")
    ck("no message to Alex", slack.lookups and not any(m["channel"] == "U_ALEX" for m in slack.sent))
    ck("Alex's list holds none of them", not [i for i in reader.waiting_for(alex, now_utc())
                                              if i.get("kind") == "question"])

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
