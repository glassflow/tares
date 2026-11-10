"""Morning status (TR-825): the standup, sent by the cell on weekday mornings.

The schedule from an injected clock: only on, only weekdays in the cell's zone, inside the window
after the set time, and once a day even if the send fails; Monday's report covers the weekend; the
date is recorded before anything is sent; a time zone moves the day. The save route is admin-only
and validates the time, the zone and that an enabled report can go somewhere; it never takes the
timer's dates from a request. "Send one now" leaves today's date alone. Slack and email each failing
alone does not stop the other. The token goes in the Authorization header and nowhere else. Private
projects are left out of a report meant for a shared place. No real Slack or mail service is called.

Run: .venv/bin/python tests/test_morning_status.py
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-morning-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-morning-status"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"
for _k in ("TARES_WORKSPACE_API_URL", "TARES_SLACK_BOT_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    os.environ.pop(_k, None)

import httpx

from tares import morning_status as ms
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# 2026-11-09 is a Monday; 09:00 Berlin is 08:00 UTC in November.
def utc(day, h, m=0):
    return datetime(2026, 11, day, h, m, tzinfo=timezone.utc)


CFG = {**ms.DEFAULTS, "enabled": True, "time": "09:00", "timezone": "Europe/Berlin",
       "to_slack": True, "channel": "C1"}


def schedule():
    print("== the schedule ==")
    ck("off never sends", not ms.due({**CFG, "enabled": False}, utc(9, 8)))
    ck("a weekday, at the set time", ms.due(CFG, utc(10, 8)))
    ck("the minute before is not due", not ms.due(CFG, utc(10, 7, 59)))
    ck("inside the window a restart still sends", ms.due(CFG, utc(10, 8, 30)))
    ck("past the window it does not", not ms.due(CFG, utc(10, 10, 0)))
    ck("a restart at 17:00 does not send", not ms.due(CFG, utc(10, 16)))
    ck("Saturday does not", not ms.due(CFG, utc(7, 8)))
    ck("Sunday does not", not ms.due(CFG, utc(8, 8)))
    ck("sent today means not again", not ms.due({**CFG, "last_sent_date": "2026-11-10"}, utc(10, 8)))
    ck("sent yesterday is not today", ms.due({**CFG, "last_sent_date": "2026-11-09"}, utc(10, 8)))
    ny = {**CFG, "timezone": "America/New_York"}
    ck("the time zone moves the moment", not ms.due(ny, utc(10, 8)) and ms.due(ny, utc(10, 14)))
    tokyo = {**CFG, "timezone": "Asia/Tokyo"}
    ck("the zone's own day counts: Friday evening UTC is Saturday in Tokyo",
       not ms.due(tokyo, utc(6, 23, 30)) and ms.due(tokyo, utc(6, 0, 30)))
    since, until = ms.report_range(CFG, utc(9, 8))
    ck("Monday's range is 72 hours", (until - since).total_seconds() == 72 * 3600, (since, until))
    since, until = ms.report_range(CFG, utc(10, 8))
    ck("any other day's range is 24 hours", (until - since).total_seconds() == 24 * 3600, (since, until))
    ck("the range is in the cell's zone", until.tzinfo is not None and until.hour == 9, until)
    r = {"projects": [{"needs_you": [1, 2]}], "quiet": [{"needs_you": 1}]}
    ck("the top line counts the whole team", ms.top_line(r) == "3 things are waiting for you.", ms.top_line(r))
    ck("one thing", ms.top_line({"projects": [{"needs_you": [1]}], "quiet": []}) == "1 thing is waiting for you.")
    ck("nothing", ms.top_line({"projects": [], "quiet": []}) == "Nothing is waiting for you.")
    p = ms.plain_text("# Crew standup\n\n**Done**\n- [T1](https://x/1) a title\n")
    ck("an email is plain text", p == "Crew standup\n\nDone\n- T1 (https://x/1) a title\n", p)


def fakes(calls, slack=None, email=None, summary=None):
    report = {"since": None, "until": None, "projects": [], "quiet": []}

    async def post_slack(channel, text, title):
        calls.append(("slack", channel, text, title))
        if slack:
            raise RuntimeError(slack)

    async def post_email(subject, text, recipients):
        calls.append(("email", subject, text, recipients))
        if email:
            raise email

    async def summarise(_report, _timeout):
        calls.append(("summarise",))
        if summary:
            raise summary
        return None, "no model"

    def report_fn(since, until):
        calls.append(("report", since, until))
        return {**report, "since": since, "until": until}

    return dict(report=report_fn, summarise=summarise, post_slack=post_slack, post_email=post_email,
                slack_connected=lambda: True)


def store_with_people():
    s = Store(os.path.join(tempfile.mkdtemp(prefix="tares-morning-store-"), "s.duckdb"))
    s.create_person("Alex", "alex@example.com")
    s.create_person("Kim", "Kim@example.com")
    return s


def set_cfg(store, **over):
    store.set_setting(ms.SETTING_KEY, json.dumps({**ms.DEFAULTS, **CFG, **over}))


async def timer():
    print("== the timer ==")
    store = store_with_people()
    calls = []
    m = ms.MorningStatus(store, **fakes(calls))
    set_cfg(store, to_email=False)
    sent = await m.tick(utc(10, 8, 1))
    ck("it sends when due", sent is True and len([c for c in calls if c[0] == "slack"]) == 1, calls)
    ck("the date is saved", ms.load(store)["last_sent_date"] == "2026-11-10", ms.load(store))
    ck("a second tick sends nothing", (await m.tick(utc(10, 8, 2))) is False)
    ck("still only one post", len([c for c in calls if c[0] == "slack"]) == 1)
    ck("a saved last result", ms.load(store)["last_result"] == "Last sent Tuesday 09:01 to Slack.",
       ms.load(store)["last_result"])
    # the date goes in before anything is sent, so a crash mid-send cannot send it twice
    calls2 = []

    async def boom(*a):
        calls2.append("post")
        raise SystemExit("the process died")

    m2 = ms.MorningStatus(store_with_people(), **{**fakes([]), "post_slack": boom})
    set_cfg(m2.store, to_email=False)
    try:
        await m2.tick(utc(10, 8, 1))
    except SystemExit:
        pass
    ck("the date is recorded before the send", ms.load(m2.store)["last_sent_date"] == "2026-11-10"
       and (await m2.tick(utc(10, 8, 5))) is False, ms.load(m2.store))
    # Monday's report reaches back over the weekend
    calls3 = []
    m3 = ms.MorningStatus(store_with_people(), **fakes(calls3))
    set_cfg(m3.store, to_email=False)
    await m3.tick(utc(9, 8))
    _, since, until = next(c for c in calls3 if c[0] == "report")
    ck("Monday's report covers 72 hours", (until - since).total_seconds() == 72 * 3600, (since, until))
    # send now leaves the scheduled day alone
    calls4 = []
    m4 = ms.MorningStatus(store_with_people(), **fakes(calls4), clock=lambda: utc(10, 8, 5))
    set_cfg(m4.store, to_email=False)
    r = await m4.send_now()
    ck("send now sends", r["ok"] is True and any(c[0] == "slack" for c in calls4), r)
    ck("send now does not touch the scheduled date", ms.load(m4.store)["last_sent_date"] == "", ms.load(m4.store))
    ck("so the scheduled one still goes", (await m4.tick(utc(10, 8, 6))) is True)
    # the facts stand alone when the summary fails
    calls5 = []
    m5 = ms.MorningStatus(store_with_people(), **fakes(calls5, summary=RuntimeError("no model")), clock=lambda: utc(10, 8, 5))
    set_cfg(m5.store, to_email=False)
    r = await m5.send_now()
    ck("a failing summary still sends the facts", r["ok"] is True and any(c[0] == "slack" for c in calls5), r)


async def destinations():
    print("== each destination fails alone ==")
    os.environ["TARES_WORKSPACE_API_URL"] = "https://console.example.com/api/workspaces/demo"
    try:
        store = store_with_people()
        calls = []
        m = ms.MorningStatus(store, **fakes(calls, slack="channel_not_found"), clock=lambda: utc(10, 8, 5))
        set_cfg(store, to_email=True, recipients=["ALEX@example.com", "kim@example.com"])
        r = await m.send_now()
        ck("Slack failing: the email still goes", r["ok"] is True and any(c[0] == "email" for c in calls), r)
        ck("the result names what failed", "Slack failed: channel_not_found" in r["result"] and "to 2 people" in r["result"], r)
        ck("the recipients keep the address the people list holds",
           next(c for c in calls if c[0] == "email")[3] == ["alex@example.com", "kim@example.com"], calls)
        calls2 = []
        store2 = store_with_people()
        m2 = ms.MorningStatus(store2, **fakes(calls2, email=ms.EmailFailed("the mail service did not accept it")),
                              clock=lambda: utc(10, 8, 5))
        set_cfg(store2, to_email=True, recipients=["alex@example.com"])
        r = await m2.send_now()
        ck("Email failing: Slack still goes", r["ok"] is True and any(c[0] == "slack" for c in calls2), r)
        ck("the result names the email failure", "Email failed: the mail service did not accept it" in r["result"], r)
        calls3 = []
        store3 = store_with_people()
        m3 = ms.MorningStatus(store3, **fakes(calls3, slack="down", email=ms.EmailFailed("down")),
                              clock=lambda: utc(10, 8, 5))
        set_cfg(store3, to_email=True, recipients=["alex@example.com"])
        r = await m3.send_now()
        ck("both failing: said so, and not sent", r["ok"] is False and r["result"].startswith("Not sent Tuesday 09:05."), r)
        ck("the last result is what the page shows", ms.load(store3)["last_result"] == r["result"])
        # a person removed since the save does not make the whole email refused
        store4 = store_with_people()
        calls4 = []
        m4 = ms.MorningStatus(store4, **fakes(calls4), clock=lambda: utc(10, 8, 5))
        set_cfg(store4, to_email=True, recipients=["alex@example.com", "gone@example.com"])
        r = await m4.send_now()
        ck("someone removed since the save is left out", next(c for c in calls4 if c[0] == "email")[3] == ["alex@example.com"], calls4)
    finally:
        os.environ.pop("TARES_WORKSPACE_API_URL", None)


async def the_token():
    print("== the token goes in the header and nowhere else ==")
    os.environ["TARES_WORKSPACE_API_URL"] = "https://console.example.com/api/workspaces/demo"
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"accepted": True, "sent": 1})

    real = httpx.AsyncClient
    ms.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(handler), **kw)
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await ms.post_email("Morning status", "body", ["alex@example.com"])
        req = seen[-1]
        ck("the address is the workspace API plus /status-email",
           str(req.url) == "https://console.example.com/api/workspaces/demo/status-email", str(req.url))
        ck("the cell's own token as Bearer", req.headers["authorization"] == f"Bearer {TOKEN}", req.headers)
        ck("the token is not in the body", TOKEN not in req.content.decode(), req.content)
        ck("nothing printed carries the token", TOKEN not in out.getvalue(), out.getvalue())

        def echoes(_req):
            return httpx.Response(500, json={"detail": f"bad token {TOKEN} here"})

        ms.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(echoes), **kw)
        try:
            await ms.post_email("s", "t", ["a@example.com"])
            ck("a refusal raises", False)
        except ms.EmailFailed as e:
            ck("even an answer that repeats the token does not reach the message", TOKEN not in str(e), str(e))

        def no(_req):
            return httpx.Response(401, json={"detail": "nope"})

        ms.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(no), **kw)
        try:
            await ms.post_email("s", "t", ["a@example.com"])
            ck("a 401 raises", False)
        except ms.EmailFailed as e:
            ck("a refused key is worded for a person", "key" in str(e) and TOKEN not in str(e), str(e))

        def down(_req):
            raise httpx.ConnectError(f"could not connect with {TOKEN}")

        ms.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(down), **kw)
        store = store_with_people()

        async def nothing(_r, _t):
            return None, None

        async def no_slack(*a):
            return None

        m = ms.MorningStatus(store, report=lambda s, u: {"since": s, "until": u, "projects": [], "quiet": []},
                             summarise=nothing, post_slack=no_slack, slack_connected=lambda: False,
                             clock=lambda: utc(10, 8, 5))
        set_cfg(store, to_slack=False, channel="", to_email=True, recipients=["alex@example.com"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r = await m.send_now()
        ck("a network failure names no token in the result", TOKEN not in json.dumps(r), r)
        ck("nor in what is logged", TOKEN not in out.getvalue(), out.getvalue())
        ck("nor in the saved last result", TOKEN not in store.get_setting(ms.SETTING_KEY))
    finally:
        ms.httpx.AsyncClient = real
        os.environ.pop("TARES_WORKSPACE_API_URL", None)


async def routes():
    print("== the routes ==")
    from tares.daemon import make_app
    app = make_app()
    calls = []
    store = app.state.store
    store.create_person("Alex", "alex@example.com")
    m = app.state.morning
    for k, v in fakes(calls).items():
        if k != "report":
            setattr(m, k, v)
    m.clock = lambda: utc(10, 8, 5)
    async with app.router.lifespan_context(app):
        admin = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                  headers={"Authorization": f"Bearer {TOKEN}"})
        anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        r = await admin.get("/api/factory/settings")
        ck("the page says it is off", r.status_code == 200 and r.json()["morning"]["enabled"] is False, r.text)
        ck("self-hosted: email is not available", r.json()["morning"]["email_available"] is False, r.text)
        put = lambda **b: admin.put("/api/factory/settings/morning", json=b)
        good = {"enabled": True, "to_slack": True, "to_email": False, "channel": "C1", "time": "09:00",
                "timezone": "Europe/Berlin", "recipients": []}
        r = await anon.put("/api/factory/settings/morning", json=good)
        ck("no key: refused", r.status_code == 401, r.status_code)
        key = (await admin.post("/api/keys", json={"name": "reader", "scopes": ["read"]})).json()["secret"]
        ro = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": f"Bearer {key}"})
        ck("a read key cannot save", (await ro.put("/api/factory/settings/morning", json=good)).status_code == 403)
        ck("a read key cannot send", (await ro.post("/api/factory/settings/morning/send")).status_code == 403)
        wr = (await admin.post("/api/keys", json={"name": "writer", "scopes": ["ingest"]})).json()["secret"]
        wc = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                               headers={"Authorization": f"Bearer {wr}"})
        ck("a writing key is not an admin either", (await wc.put("/api/factory/settings/morning", json=good)).status_code == 403)
        for label, body, needle in [
            ("a bad time", {**good, "time": "9am"}, "time"),
            ("an hour out of range", {**good, "time": "25:00"}, "time"),
            ("a bad time zone", {**good, "timezone": "Mars/Base"}, "time zone"),
            ("no destination", {**good, "to_slack": False}, "where"),
            ("no channel", {**good, "channel": ""}, "channel"),
            ("email where it is not available", {**good, "to_slack": False, "to_email": True, "recipients": ["alex@example.com"]}, "not available"),
            ("a non-boolean flag", {**good, "enabled": "yes"}, "true or false"),
        ]:
            r = await put(**body)
            ck(f"refused: {label}", r.status_code == 400 and needle in r.json()["detail"], r.text)
        r = await put(**good)
        ck("a good save", r.status_code == 200 and r.json()["enabled"] is True, r.text)
        r = await put(**{**good, "last_sent_date": "2026-11-10", "last_result": "forged"})
        ck("the dates are the timer's, not the request's",
           ms.load(store)["last_sent_date"] == "" and ms.load(store)["last_result"] == "", ms.load(store))
        m.slack_connected = lambda: False
        r = await put(**good)
        ck("Slack gone: not usable", r.status_code == 400 and "Slack is not connected" in r.json()["detail"], r.text)
        m.slack_connected = lambda: True
        r = await put(**{**good, "enabled": False, "to_slack": False, "channel": ""})
        ck("off can be saved with nowhere to send", r.status_code == 200, r.text)
        r = await admin.post("/api/factory/settings/morning/send")
        ck("send now with nowhere to send is refused", r.status_code == 400, r.text)
        await put(**good)
        os.environ["TARES_WORKSPACE_API_URL"] = "https://console.example.com/api/workspaces/demo"
        try:
            r = await put(**{**good, "to_slack": False, "channel": "", "to_email": True, "recipients": ["ALEX@example.com"]})
            ck("email only on a cloud cell, Slack unconnected", r.status_code == 200 and r.json()["recipients"] == ["alex@example.com"], r.text)
            r = await put(**{**good, "to_slack": False, "channel": "", "to_email": True, "recipients": ["stranger@example.com"]})
            ck("a recipient not on the cell is refused", r.status_code == 400 and "stranger" in r.json()["detail"], r.text)
            r = await put(**{**good, "to_slack": False, "channel": "", "to_email": True, "recipients": []})
            ck("email with nobody picked is refused", r.status_code == 400, r.text)
            ck("the page says email is available", (await admin.get("/api/factory/settings")).json()["morning"]["email_available"] is True)
        finally:
            os.environ.pop("TARES_WORKSPACE_API_URL", None)
        await put(**good)
        r = await admin.post("/api/factory/settings/morning/send")
        ck("send now answers the result line", r.status_code == 200 and r.json()["ok"] is True and "Last sent Tuesday" in r.json()["result"], r.text)
        ck("the page shows the last result", (await admin.get("/api/factory/settings")).json()["morning"]["last_result"].startswith("Last sent Tuesday"))

        # a real standup report: a private project stays out of it
        px = (await admin.post("/api/projects", json={"template": "custom", "name": "open-project", "objects": [], "goal": "g", "kind": "software_factory"})).json()["id"]
        sx = (await admin.post("/api/projects", json={"template": "custom", "name": "secret-project", "objects": [], "goal": "g", "kind": "software_factory"})).json()["id"]
        store.set_project_scope(sx, "private")
        hidden = {p["id"] for p in store.private_projects()}
        ck("the report's hide set holds the private project", sx in hidden and px not in hidden, hidden)
        since, until = ms.report_range(CFG, utc(10, 8))
        report = await asyncio.to_thread(m.report, since, until)
        names = [p["name"] for p in report["projects"]] + [q["name"] for q in report["quiet"]]
        ck("a private project is not in the report", "secret-project" not in names, names)
        await admin.aclose(); await anon.aclose(); await ro.aclose(); await wc.aclose()


async def main():
    schedule()
    await timer()
    await destinations()
    await the_token()
    await routes()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
