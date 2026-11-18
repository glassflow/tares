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
  - more than five new items: five messages and one that says how many more;
  - an email Slack does not know, and a bot without users:read.email, send nothing and say so.

Run: .venv/bin/python tests/test_waiting_slack.py
"""
import asyncio
import hashlib
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
        self.users = {"alex@example.com": "U_ALEX", "kim@example.com": "U_KIM"}
        self.lookups = 0
        self.error: str | None = None

    async def send(self, token, msg):
        self.sent.append(msg)
        return True, None

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
    reader._send, reader._lookup, reader.clock = slack.send, slack.lookup, lambda: clock["now"]

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

    async def read(minutes=1):
        clock["now"] = clock["now"] + timedelta(minutes=minutes)
        return await reader.read_once()

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

    print("== many new at once: five, then one with how many more ==")
    for n in range(8):
        ask(f"Question {n}?", to=kim["id"])
    await read()
    sent = slack.take()
    ck("six messages", len(sent) == 6, len(sent))
    ck("the last says 3 more", sent and sent[-1]["text"] == "3 more things wait for you.", sent and sent[-1]["text"])
    ck("which links to the feed", sent and "https://cell.example.com/feed'" in str(sent[-1]["blocks"]),
       sent and sent[-1]["blocks"])
    await read()
    ck("and none of them again", slack.take() == [])

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
    async with cx(secret) as c:
        r = await c.get("/api/me/slack")
        ck("Settings names the missing scope", "users:read.email" in str(r.json()["problem"]), r.text)
    slack.error = "ratelimited"
    ask("Add search?", to=kim["id"])
    await read()
    ck("Slack could not say: nothing sent yet", slack.take() == [])
    slack.error = None
    await read()
    ck("the next read sends what waited for the lookup", [m["text"] for m in slack.take()]
       == ["Waiting for you: Add search?"])

    print("== someone who joins later: what already waits for them is not sent ==")
    lee = store.create_person("Lee", email="lee@example.com")
    slack.users["lee@example.com"] = "U_LEE"
    ask("Pick the payment provider?", to=lee["id"])
    await read()
    ck("Lee's first read sends nothing", slack.take() == [])
    ask("Add Apple Pay?", to=lee["id"])
    await read()
    ck("Lee's next new one goes", [m["channel"] for m in slack.take()] == ["U_LEE"])

    print("== Alex was never told about Kim's questions ==")
    ck("no message to Alex", slack.lookups and not any(m["channel"] == "U_ALEX" for m in slack.sent))
    ck("Alex's list holds none of them", not [i for i in reader.waiting_for(alex, now_utc())
                                              if i.get("kind") == "question"])

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
