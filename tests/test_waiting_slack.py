"""A direct message on Slack when something new waits for a person (TR-1458).

The reader reads each person's "Waiting for you" (factory_api.waiting_for_person) and sends one
message per new item: its ask, what it holds up, and a link to the feed on that item. Slack's
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

from tares import waiting_slack as ws
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
    link = f"https://cell.example.com/feed?item={item['id'].replace(':', '%3A')}"
    ck("it links to the feed on that item", link in body, (link, body))
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
    ck("it links to the feed", "https://cell.example.com/feed'" in body, body)
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
    ck("and no longer offers the button", "open_feed" not in str(e.get("blocks")), e)
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
    slack.send_error = ("slack: channel_not_found", False)
    await read()
    slack.send_error = None
    await read()
    ck("a refusal that will not change is not tried every minute",
       len(slack.refused) == 1 and slack.take() == [], slack.refused)
    for d in (busy, gone):      # off Alex's list again, for the checks below
        store.close_desk_item(d["id"], "dismissed", None, now_utc())
    await read()
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
