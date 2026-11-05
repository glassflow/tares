"""The factory Crew page (TR-736): GET /api/factory/crew.

Who is on the crew by role, each member's state in words, builders grouped by project (an ended
builder of a finished project left out, helpers counted under their builder), how far the crew may
go, the pacing sentence, the plan with the share of each project, the rule proposals and the
standup, plus the cases that must not break the page: nothing started, a station never heard from,
text with markup in it. The body is checked against tares/words.py.

Run: .venv/bin/python tests/test_factory_crew_page.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-crew-page-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-crew-page"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx

import tares.mcp_factory as mf
import tares.mcp_server as m
from tares import words
from tares.screens import factory_crew as fc

P = F = 0

MEMBER_KEYS = {"station", "role_label", "state", "state_text", "doing", "heard", "memory_pct",
               "memory_text", "helpers"}


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(seconds_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def st_line(sid, station, role, parent="", project="", ago=3600):
    o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "station": station, "role": role, "parent": parent, "factory_station": station}
    if project:
        o["tares_project"] = project
    return o


def state(sid, s, ago, reason=""):
    return {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
            "state": s, "reason": reason}


def context(sid, pct, ago=5):
    return {"type": "session_status", "sessionId": sid, "timestamp": ts(ago),
            "status": {"session_id": sid, "context_window": {
                "context_window_size": 200000, "used_percentage": pct,
                "current_usage": {"input_tokens": 2, "output_tokens": 40,
                                  "cache_creation_input_tokens": 300, "cache_read_input_tokens": 90000}}}}


def reply(sid, mid, ago, project="", text="ok"):
    o = {"type": "assistant", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5", "content": text,
                     "usage": {"input_tokens": 2, "output_tokens": 10,
                               "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 50000,
                               "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                                  "ephemeral_5m_input_tokens": 1000}}}}
    if project:
        o["tares_project"] = project
    return o


def plan_line(session_pct, weekly_pct):
    now = datetime.now(timezone.utc)
    return {"type": "claude_plan", "timestamp": ts(1), "usage_report": {
        "session": {"total_cost_usd": 0},
        "rate_limits": {"limits": [
            {"kind": "session", "group": "session", "percent": session_pct, "scope": None,
             "resets_at": (now + timedelta(hours=2)).isoformat(), "severity": "normal",
             "is_active": True},
            {"kind": "weekly_all", "group": "weekly", "percent": weekly_pct, "scope": None,
             "resets_at": (now + timedelta(days=2)).isoformat(), "severity": "normal",
             "is_active": False}],
            "extra_usage": {"is_enabled": False, "monthly_limit": None}}}}


def units():
    print("== the helpers ==")
    ck("a cost in dollars, or under one", [fc._cost(12.4), fc._cost(0.2), fc._cost(None), fc._cost("x")]
       == ["$12 at API prices", "under $1 at API prices", "", ""], fc._cost(12.4))
    ck("how far: L3 with a releaser and no limit", fc._how({"autonomy": None, "releaser": None}) ==
       {"text": "You approve each merge.", "detail": "Releases wait for you.",
        "settings_href": "/settings#crew"}, fc._how({"autonomy": None, "releaser": None}))
    ck("how far: L4 with no releaser and 4 builders", fc._how(
        {"autonomy": "L4-ship", "releaser": False, "builders_max": 4}) ==
       {"text": "The crew merges after its review and green CI.",
        "detail": "No releases. Up to 4 builders at once.", "settings_href": "/settings#crew"})
    ck("how far: one builder, a junk limit is ignored",
       fc._how({"releaser": True, "builders_max": 1})["detail"] == "Releases wait for you. Up to 1 builder at once."
       and fc._how({"releaser": True, "builders_max": "x"})["detail"] == "Releases wait for you."
       and fc._how({"releaser": True, "builders_max": True})["detail"] == "Releases wait for you.")
    now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    ck("no pacing sentence while pacing is off or the plan has room",
       fc._pacing({"on": False, "level": 1}, now) is None and fc._pacing({"on": True, "level": 0}, now) is None
       and fc._pacing(None, now) is None)
    ck("pacing, slow", fc._pacing({"on": True, "level": 1, "session_pct": 86, "weekly_pct": 61,
                                   "until": "2026-11-02T21:50:00Z"}, now) ==
       "Pacing: the 5-hour window is at 86%, so no new builder starts until 21:50.")
    ck("pacing, hold with both limits and no end time",
       fc._pacing({"on": True, "level": 2, "session_pct": 96, "weekly_pct": 91, "until": None}, now) ==
       "Pacing: the 5-hour window is at 96% and this week's limit is at 91%, so builders finish their "
       "step and wait, and no new builder starts.")


async def main():
    from tares.daemon import make_app
    app = make_app()
    m.TARESD = mf.TARESD = "http://t"
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                   headers={"Authorization": f"Bearer {TOKEN}"})
    m._cx = mf._cx = mk
    store = app.state.store

    async def ingest(cx, lines):
        r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})
        assert r.status_code < 300, r.text

    async def page(cx):
        r = await cx.get("/api/factory/crew")
        assert r.status_code == 200, r.text
        return r.json()

    def clean(label, body):
        found = words.offenders_in(body, ("name", "doing", "text"))
        ck(label + ": no forbidden word or dash outside the names and the crew's own words", found == [], found)

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            print("== a cell where the crew never started ==")
            r = await cx.get("/api/factory/crew")
            a = r.json()
            ck("the page answers", r.status_code == 200, r.text)
            ck("every key of the shape", set(a) == {"summary", "talk_to", "reviewer", "releaser", "builders",
               "how_far", "pacing", "plan", "rule_proposals", "standup", "now"}, sorted(a))
            ck("nobody on the crew, and the summary says it was never started",
               a["summary"] == "Not started" and a["talk_to"] is None and a["reviewer"] is None
               and a["releaser"] is None and a["builders"] == [], a)
            ck("no pacing, no rules, no standup, the plan unknown",
               a["pacing"] is None and a["rule_proposals"] == [] and a["standup"] is None
               and a["plan"]["known"] is False and a["plan"]["limits"] == []
               and a["plan"]["by_project"] == [], a["plan"])
            ck("how far it may go has its words and the link to Settings",
               a["how_far"] == {"text": "You approve each merge.", "detail": "Releases wait for you.",
                                "settings_href": "/settings#crew"}, a["how_far"])
            clean("an empty cell", a)

            print("== a crew at work ==")
            px = json.loads(await m.create_project("project-x", "Ship the docs site"))["id"]
            await mf.write_milestone("M1: first pages", project="project-x")
            for t in ("Home page", "Footer", "Search box", "Search index"):
                await m.write_ticket(t, milestone="M1: first pages", project="project-x",
                                     repo="example-org/docs-site")
            py = json.loads(await m.create_project("project-y", "Move the blog"))["id"]
            await m.write_ticket("Export", project="project-y")
            pd = json.loads(await m.create_project("project-d", "Old work"))["id"]
            await m.write_ticket("Old", project="project-d")
            for p in (px, py, pd):
                await cx.post(f"/api/projects/{p}/handover",
                              json={"repos": [{"path": "/w/docs-site", "name": "example-org/docs-site"}]})
            for n in ("T1",):
                assert (await cx.post(f"/api/projects/{pd}/tickets/{n}/stage",
                                      json={"stage": "merged"})).status_code == 200
            assert (await cx.post(f"/api/projects/{pd}/finish", json={"words": "done"})).status_code == 200
            assert (await cx.post(f"/api/projects/{px}/tickets/T1/stage",
                                  json={"stage": "merged"})).status_code == 200
            sh = {"X-Tares-Station": "project-x-build-m1", "X-Tares-Role": "builder"}
            await cx.post(f"/api/projects/{px}/tickets/T2/assign", json={"holder": "project-x-build-m1"})
            assert (await cx.post(f"/api/projects/{px}/tickets/T2/stage", json={"stage": "doing"},
                                  headers=sh)).status_code == 200
            await cx.post(f"/api/projects/{px}/tickets/T3/assign", json={"holder": "project-x-build-m2"})
            assert (await cx.post(f"/api/projects/{px}/tickets/T3/stage", json={"stage": "doing"},
                                  headers={"X-Tares-Station": "project-x-build-m2",
                                           "X-Tares-Role": "builder"})).status_code == 200
            assert (await cx.post(f"/api/projects/{px}/tickets/T3/stage", json={"stage": "review"},
                                  headers={"X-Tares-Station": "project-x-build-m2",
                                           "X-Tares-Role": "builder"})).status_code == 200
            await ingest(cx, [
                st_line("o1", "crew-orchestrator", "orchestrator"), state("o1", "working", 12),
                context("o1", 82), reply("o1", "o-1", 30),
                st_line("r1", "crew-reviewer", "reviewer"), state("r1", "idle", 70),
                context("r1", 41),
                st_line("l1", "crew-releaser", "releaser"), state("l1", "idle", 7200),
                st_line("b1", "project-x-build-m1", "builder", "crew-orchestrator", project=px),
                state("b1", "working", 20), context("b1", 58), reply("b1", "m1", 30, px),
                reply("b1", "m2", 25, px, "Reading <script>alert(1)</script> & more"),
                st_line("h1", "project-x-build-m1-lint", "helper", "project-x-build-m1", project=px),
                state("h1", "working", 10),
                st_line("h2", "project-x-build-m1-old", "helper", "project-x-build-m1", project=px),
                state("h2", "ended", 100),
                st_line("b2", "project-x-build-m2", "builder", "crew-orchestrator", project=px),
                state("b2", "idle", 480), context("b2", 63),
                st_line("b3", "project-x-build-old", "builder", "crew-orchestrator", project=px),
                state("b3", "ended", 86400),
                st_line("b4", "project-d-build-m1", "builder", "crew-orchestrator", project=pd),
                state("b4", "ended", 172800),
                st_line("b5", "project-y-build-m1", "builder", "crew-orchestrator", project=py, ago=7200),
                state("b5", "working", 7200), context("b5", 88),
                plan_line(1, 11),
            ])
            a = await page(cx)
            print("   ", json.dumps(a)[:300])
            t, rv, rl = a["talk_to"], a["reviewer"], a["releaser"]
            ck("the orchestrator: role, working, with its memory", t["role_label"] == "Orchestrator"
               and t["state"] == "working" and t["state_text"] == "Working" and t["memory_pct"] == 82
               and t["memory_text"] == "Memory 82% full" and t["helpers"] == 0, t)
            ck("heard is to the second under a minute", t["heard"].endswith(" seconds ago"), t["heard"])
            ck("the summary says the lead works and when it was heard",
               a["summary"].startswith("Working, last heard ") and a["summary"].endswith(" ago"), a["summary"])
            ck("the reviewer: idle for a minute",
               rv["role_label"] == "Reviewer" and rv["state"] == "idle" and rv["state_text"] == "Idle for 1 minute"
               and rv["memory_text"] == "Memory 41% full", rv)
            ck("the releaser: shown while the setting is not off, with no memory reading",
               rl["role_label"] == "Releaser" and rl["state_text"] == "Idle for 2 hours"
               and rl["memory_pct"] is None and rl["memory_text"] is None, rl)
            ck("every member has exactly the shared keys and no version",
               all(set(x) == MEMBER_KEYS for x in [t, rv, rl] + [b for g in a["builders"] for b in g["members"]])
               and "claude_version" not in json.dumps(a), "")
            groups = {g["project"]["name"]: g for g in a["builders"]}
            ck("builders are grouped by project, in order of name, a finished project's ended builder left out",
               [g["project"]["name"] for g in a["builders"]] == ["project-x", "project-y"]
               and "project-d" not in groups and groups["project-x"]["project"]["id"] == px,
               [g["project"]["name"] for g in a["builders"]])
            xs = {b["station"]: b for b in groups["project-x"]["members"]}
            ck("a project's builders: working, waiting for the review, ended; helpers counted not listed",
               sorted(xs) == ["project-x-build-m1", "project-x-build-m2", "project-x-build-old"]
               and xs["project-x-build-m1"]["state"] == "working" and xs["project-x-build-m1"]["helpers"] == 1
               and xs["project-x-build-m1"]["doing"] == "Working on Footer"
               and xs["project-x-build-m1"]["role_label"] == "Builder", xs)
            b2 = xs["project-x-build-m2"]
            ck("an idle builder with a ticket in review says what it waits for",
               b2["state"] == "idle" and b2["state_text"] == "Idle for 8 minutes"
               and b2["doing"] == "Waiting for the review of Search box" and b2["memory_pct"] == 63, b2)
            old = xs["project-x-build-old"]
            ck("an ended builder of an open project stays, with no memory reading",
               old["state"] == "ended" and old["state_text"] == "Ended 1 day ago"
               and old["doing"] == "Finished its work." and old["memory_pct"] is None
               and old["memory_text"] is None and old["helpers"] == 0, old)
            y = groups["project-y"]["members"][0]
            ck("a builder not heard from for two hours is 'not heard'",
               y["state"] == "not heard" and y["state_text"] == "Not heard from in 2 hours"
               and y["memory_text"] == "Memory 88% full", y)
            ck("the plan, with its by-project share and what it cost",
               a["plan"]["known"] is True and [x["label"] for x in a["plan"]["limits"]] ==
               ["Current 5 hours", "This week"]
               and a["plan"]["limits"][0]["text"].startswith("1% used, resets ")
               and a["plan"]["limits"][1]["text"].startswith("11% used, resets ")
               and a["plan"]["by_project"] and all(set(x) == {"name", "share_text", "cost_text"}
                                                   for x in a["plan"]["by_project"])
               and a["plan"]["by_project"][0]["share_text"].endswith("%")
               and a["plan"]["by_project"][0]["cost_text"].endswith(" at API prices"), a["plan"])
            ck("no pacing while the plan has room", a["pacing"] is None, a["pacing"])
            ck("the standup counts what moved in the last day",
               a["standup"] is not None and a["standup"]["text"].startswith("In the last day: ")
               and "project-x" in a["standup"]["text"] and a["standup"]["when"] == "The last 24 hours",
               a["standup"])
            clean("a crew at work", a)

            print("== the lines a member shows come from the member route ==")
            r = await cx.get("/api/crew/project-x-build-m1?recent=30")
            d = r.json()
            ck("the detail has the lines, kept as text, and the attach command",
               r.status_code == 200 and d["attach"] == "tares factory crew attach project-x-build-m1"
               and any("<script>" in x["text"] for x in d["recent"]), d.get("attach"))
            r = await cx.post("/api/crew/project-x-build-m1/context")
            ck("a new reading of its memory can be asked for", r.status_code == 200 and "asked_at" in r.json(),
               r.text)

            print("== how far it may go, the releaser, pacing ==")
            r = await cx.put("/api/crew/settings", json={"autonomy": "L4-ship", "releaser": False,
                                                         "builders_max": 4, "pace": True})
            ck("the settings are changed", r.status_code == 200, r.text)
            a = await page(cx)
            ck("L4 with no releaser: the releaser leaves the page and the words follow",
               a["releaser"] is None and a["how_far"]["text"] == "The crew merges after its review and green CI."
               and a["how_far"]["detail"] == "No releases. Up to 4 builders at once.", a["how_far"])
            await ingest(cx, [plan_line(86, 61)])
            a = await page(cx)
            ck("pacing on with the window at 86%: the sentence names it and when it ends",
               a["pacing"] is not None and a["pacing"].startswith("Pacing: the 5-hour window is at 86%, so no "
               "new builder starts until ") and a["pacing"].endswith("."), a["pacing"])
            ck("and the plan shows the new numbers",
               a["plan"]["limits"][0]["text"].startswith("86% used, resets "), a["plan"]["limits"])
            clean("a paced crew", a)

            print("== rule proposals ==")
            store.upsert_rule_proposal("example-org/docs-site", "missing tests", py, "project",
                                       "In example-org/docs-site, check for missing tests before asking "
                                       "for review: it came back in 3 tickets.", ["T1", "T2"],
                                       datetime.now(timezone.utc) - timedelta(days=2))
            store.upsert_rule_proposal("example-org/service-a", "<b>markup</b>", None, "all",
                                       "Never merge <i>on a red check</i>.", ["T9"],
                                       datetime.now(timezone.utc) - timedelta(hours=6))
            a = await page(cx)
            props = a["rule_proposals"]
            ck("the open ones, each with who proposed it and when",
               len(props) == 2 and all(set(p) == {"id", "text", "from", "when"} for p in props)
               and all(p["from"] == "the reviewer" for p in props)
               and sorted(p["when"] for p in props) == ["2 days ago", "6 hours ago"], props)
            ck("text with markup comes through as it was written", any("<i>on a red check</i>" in p["text"]
                                                                       for p in props), props)
            first = props[0]["id"]
            r = await cx.post(f"/api/rule-proposals/{first}/reject")
            ck("a rule is rejected with the route that exists", r.status_code == 200
               and r.json()["state"] == "rejected", r.text)
            ck("and then it leaves the page", [p["id"] for p in (await page(cx))["rule_proposals"]]
               == [p["id"] for p in props if p["id"] != first])

            print("== a crew that ended ==")
            await ingest(cx, [state("o1", "ended", 1)])
            a = await page(cx)
            ck("the lead ended: not running, and the member is ended",
               a["summary"] == "Not running" and a["talk_to"]["state"] == "ended"
               and a["talk_to"]["memory_pct"] is None, (a["summary"], a["talk_to"]))
            clean("a quiet crew", a)

            print("== a station that waits ==")
            await ingest(cx, [st_line("o1", "crew-orchestrator", "orchestrator", ago=1), state("o1", "waiting", 1,
                                                                                                "4 reviewed pull requests wait")])
            a = await page(cx)
            ck("a waiting lead shows as 'waiting for you' and says why",
               a["talk_to"]["state"] == "waiting for you" and a["talk_to"]["state_text"] == "Waiting for you"
               and a["talk_to"]["doing"] == "4 reviewed pull requests wait", a["talk_to"])


async def holes():
    """A cell of its own: a lead gone quiet, and values the crew did not report."""
    import tares.daemon as daemon_mod
    daemon_mod.DB_PATH = os.path.join(_TMP, "holes.duckdb")   # a database of its own
    from tares.daemon import make_app
    from tares.factory_home import crew_member
    app = make_app()
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                              headers={"Authorization": f"Bearer {TOKEN}"})
    print("== a lead that went quiet ==")
    async with app.router.lifespan_context(app):
        async with mk() as cx:
            r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in [
                st_line("o1", "crew-orchestrator", "orchestrator", ago=7200), state("o1", "idle", 7200)]) + "\n",
                headers={"Content-Type": "application/x-ndjson"})
            assert r.status_code < 300, r.text
            a = (await cx.get("/api/factory/crew")).json()
            ck("silent for two hours: the summary and the member say so",
               a["summary"] == "Not heard from in 2 hours" and a["talk_to"]["state"] == "not heard"
               and a["talk_to"]["state_text"] == "Not heard from in 2 hours"
               and a["talk_to"]["heard"] == "2 hours ago", (a["summary"], a["talk_to"]))
            ck("and the page is still free of forbidden words",
               words.offenders_in(a, ("name", "doing", "text")) == [])
    print("== missing and odd values ==")
    odd = [
        {"name": "crew-orchestrator", "role": "orchestrator", "state": "idle", "parent": None,
         "project": None, "project_name": None, "state_reason": None, "state_at": None,
         "last_at": None, "context": None, "children": []},
        {"name": "crew-reviewer", "role": "reviewer", "state": "working", "parent": None,
         "project": None, "project_name": None, "state_reason": "", "state_at": None,
         "last_at": None, "context": {"pct": None}, "children": None},
        {"name": "x-build", "role": "builder", "state": "working", "parent": None,
         "project": "gone", "project_name": None, "state_reason": None, "state_at": None,
         "last_at": None, "context": {"pct": "n/a"}, "children": ["nope"]},
        {"name": "h-1", "role": "helper", "state": "idle", "parent": "x-build", "project": None,
         "project_name": None, "state_reason": None, "state_at": None, "last_at": None,
         "context": None, "children": []},
    ]
    now = datetime.now(timezone.utc)
    by = {s["name"]: s for s in odd}
    odd[2]["children"] = ["h-1", "nope"]
    ms = [crew_member(s, {}, 0, now, by) for s in odd[:3]]
    ck("a station never heard from has no heard line and no memory, and still has its words",
       ms[0]["heard"] == "" and ms[0]["state_text"] == "Idle" and ms[0]["memory_pct"] is None
       and ms[1]["memory_text"] is None and ms[2]["memory_pct"] is None, ms)
    ck("only helpers count as helpers, a name nobody reported is not one", ms[2]["helpers"] == 1, ms[2])


units()
asyncio.run(main())
asyncio.run(holes())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
