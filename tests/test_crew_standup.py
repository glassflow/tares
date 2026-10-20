"""The crew's standup report: what moved in each handed-over project over a time range.

Done, started, in review and blocked come from the ticket history inside the range; the
questions on the desk are listed as needing the person; a project with nothing in the range is
named under "no movement"; a range before the work shows nothing moved. The summary endpoint
works without a model provider (the report comes back with summary_error), and the model's JSON
is read even inside a code fence. Posting to Slack is refused from a crew station and without a
bot token.

Run: .venv/bin/python tests/test_crew_standup.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-crew-standup-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-standup"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
          "TARES_PLATFORM_PROVIDER_KEY", "TARES_SLACK_BOT_TOKEN"):
    os.environ.pop(k, None)

import httpx

import tares.mcp_server as m
import tares.mcp_factory as mf
from tares import standup

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def iso(d):
    return d.isoformat()


async def main():
    from tares.daemon import make_app
    app = make_app()
    store = app.state.store
    root = {"Authorization": f"Bearer {TOKEN}"}
    m.TARESD = mf.TARESD = "http://t"
    headers = dict(root)
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t", headers=dict(headers))
    m._cx = mf._cx = mk

    def as_station(station="", role="", parent=""):
        headers.clear()
        headers.update(root)
        if station:
            headers.update({"X-Tares-Station": station, "X-Tares-Role": role})
        if parent:
            headers["X-Tares-Parent"] = parent

    async with app.router.lifespan_context(app):
        shop = json.loads(await m.create_project("Shop", "Sell things"))["id"]
        json.loads(await m.create_project("Blog", "Write things"))
        for t in ("Catalog", "Cart", "Checkout", "Search"):
            await m.write_ticket(t, project="Shop")
        await m.write_ticket("Posts", project="Blog")
        await mf.hand_over(project="Shop", repo="/w/shop")
        await mf.hand_over(project="Blog", repo="/w/blog")
        before = datetime.now(timezone.utc)

        as_station("crew-orchestrator", "orchestrator")
        for ref in ("T1", "T2", "T3"):
            await mf.assign_ticket(ref, "shop-build-m1", project="Shop")
        as_station("shop-build-m1", "builder", "crew-orchestrator")
        await mf.set_stage("T1", "doing", project="Shop")
        await mf.set_stage("T1", "merged", project="Shop")
        await mf.set_stage("T2", "doing", project="Shop")
        await mf.set_stage("T3", "doing", project="Shop")
        await mf.set_stage("T3", "blocked", reason="waiting on the payment API key",
                           project="Shop")
        as_station("crew-orchestrator", "orchestrator")
        async with mk() as cx:
            r = await cx.post("/api/desk", json={
                "question": "Which payment provider?", "recommendation": "Stripe",
                "why": "already used", "project": shop, "blocking": True})
        ck("a desk question is asked", r.status_code == 201, r.text)
        as_station()

        print("== the report ==")
        async with mk() as cx:
            r = await cx.get("/api/crew/standup")
        ck("GET /api/crew/standup answers (not taken by /api/crew/{name})", r.status_code == 200,
           r.text)
        rep = r.json()
        names = [p["name"] for p in rep["projects"]]
        ck("Shop moved; Blog is under no movement",
           names == ["Shop"] and [q["name"] for q in rep["quiet"]] == ["Blog"], rep)
        p = rep["projects"][0]
        ck("T1 done, T2 started, T3 blocked with its reason",
           [x["ref"] for x in p["done"]] == ["T1"]
           and [x["ref"] for x in p["started"]] == ["T2"]
           and [(x["ref"], x["reason"], x["new"]) for x in p["blocked"]]
           == [("T3", "waiting on the payment API key", True)], p)
        ck("the open desk question needs the person",
           [(d["question"], d["blocking"]) for d in p["needs_you"]]
           == [("Which payment provider?", True)], p["needs_you"])
        ck("progress counts the project's tickets", p["progress"]["done"] == 1
           and p["progress"]["total"] == 4, p["progress"])
        md = rep["markdown"]
        ck("the markdown has the project's heading and groups",
           "## Shop: 1/4 done" in md and "**Done**" in md and "**Blocked**" in md
           and "**Needs you**" in md and "**No movement:** Blog (0/1)" in md, md)
        ck("no em dashes in the report", "\u2014" not in md, md)

        async with mk() as cx:
            r = await cx.get("/api/crew/standup", params={
                "since": iso(before - timedelta(days=3)), "until": iso(before - timedelta(days=2))})
        old = r.json()
        ck("a range before the work: nothing done, nothing started",
           all(not p["done"] and not p["started"] for p in old["projects"]), old)
        async with mk() as cx:
            bad = await cx.get("/api/crew/standup", params={"since": iso(before),
                                                            "until": iso(before - timedelta(1))})
            bad2 = await cx.get("/api/crew/standup", params={"since": "yesterday"})
        ck("since after until is refused", bad.status_code == 400, bad.text)
        ck("a since that is not a date is refused", bad2.status_code == 400, bad2.text)

        print("== the summary ==")
        async with mk() as cx:
            r = await cx.post("/api/crew/standup/summary", json={})
        s = r.json()
        ck("without a model provider the report still comes back, with the reason",
           r.status_code == 200 and s["summary"] is None
           and "no model provider" in (s["summary_error"] or "") and "## Shop" in s["markdown"], s)
        import tares.daemon as daemon_mod
        from tares.models import ModelReply, empty_usage
        seen = {}

        class FakeProvider:
            kind = "anthropic"

            async def complete(self, **kw):
                seen.update(kw)
                return ModelReply(text=json.dumps({"overall": "Shop shipped T1.",
                                                   "projects": {shop: "T1 merged; T3 waits on a key."}}),
                                  tool_calls=[], usage=empty_usage())

        real = daemon_mod.resolve_provider
        daemon_mod.resolve_provider = lambda store: (FakeProvider(), "test")
        try:
            async with mk() as cx:
                r = await cx.post("/api/crew/standup/summary", json={})
        finally:
            daemon_mod.resolve_provider = real
        s = r.json()
        ck("with a model the summary sits in the markdown under the project",
           s["summary_error"] is None and s["summary"]["projects"] == {shop: "T1 merged; T3 waits on a key."}
           and "## Shop: 1/4 done\n\nT1 merged; T3 waits on a key." in s["markdown"]
           and "Shop shipped T1." in s["markdown"], s)
        ck("the model gets the facts without links, and no tools",
           "Which payment provider?" in seen["messages"][0]["content"]
           and "github.com" not in seen["messages"][0]["content"] and seen["tools"] == [], seen)
        parsed = standup.parse_summary(
            '```json\n{"overall": "One shipped \u2014 one stuck.", "projects": {"%s": "T1 merged.",'
            ' "nope": "x"}}\n```' % shop, {shop})
        ck("the model's JSON is read inside a fence; unknown ids dropped; em dash replaced",
           parsed == {"overall": "One shipped, one stuck.", "projects": {shop: "T1 merged."}},
           parsed)
        ck("unreadable model output gives an empty summary",
           standup.parse_summary("not json", {shop}) == {"overall": None, "projects": {}})
        dated = {**rep, "since": datetime.fromisoformat(rep["since"]),
                 "until": datetime.fromisoformat(rep["until"])}
        md = standup.markdown(dated, parsed)
        ck("the summary sits under the project's heading",
           "## Shop: 1/4 done\n\nT1 merged." in md and "One shipped" in md, md)

        print("== Slack ==")
        as_station("crew-orchestrator", "orchestrator")
        async with mk() as cx:
            r = await cx.post("/api/slack/post", json={"channel": "C1", "text": "hi"})
        ck("a crew station may not post to Slack", r.status_code == 403, r.text)
        as_station()
        async with mk() as cx:
            r = await cx.post("/api/slack/post", json={"channel": "C1", "text": "hi"})
            r2 = await cx.post("/api/slack/post", json={"channel": "", "text": "hi"})
        ck("without a bot token it says Slack is not connected",
           r.status_code == 409 and "not connected" in r.text, r.text)
        ck("a channel is required", r2.status_code == 400, r2.text)


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
