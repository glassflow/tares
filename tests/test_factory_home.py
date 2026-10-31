"""The factory home in one call (TR-729): GET /api/home on a factory cell.

A headline, the open projects, the crew, the Claude plan and the connected tools, worded for
people, and the one shape of a "waiting for you" item. The helpers the screens import
(plain_wait, plain_ago, home_item, project_state, project_row, crew_member) are checked here too.

Run: .venv/bin/python tests/test_factory_home.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-factory-home-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-home"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["TARES_EDITION"] = "factory"

import httpx
from fastapi import FastAPI

import tares.mcp_factory as mf
import tares.mcp_server as m
from tares import factory_api
from tares import factory_home as fh
from tares import words

# this test is about the frame: it fills the builders itself, so the real ones
# (tares/factory_waiting.py, tested in test_factory_home_waiting.py) stay out
fh.WAITING_BUILDERS.clear()

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def st_line(sid, station, role, parent="", project="", ago=30):
    o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "station": station, "role": role, "parent": parent, "factory_station": station}
    if project:
        o["tares_project"] = project
    return o


def state(sid, s, ago, reason=""):
    return {"type": "session_state", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
            "state": s, "reason": reason}


def reply(sid, mid, ago, project=""):
    o = {"type": "assistant", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "message": {"id": mid, "role": "assistant", "model": "claude-opus-5-5", "content": "ok",
                     "usage": {"input_tokens": 2, "output_tokens": 10,
                               "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 50000,
                               "cache_creation": {"ephemeral_1h_input_tokens": 0,
                                                  "ephemeral_5m_input_tokens": 1000}}}}
    if project:
        o["tares_project"] = project
    return o


def tk(i, status="todo", stage=None, **kw):
    return {"id": f"tk_{i}", "status": status, "stage": stage, "milestone": None, "depends_on": [],
            "title": f"Ticket {i}", **kw}


def item(kind, project=None, since=None, **kw):
    now = datetime.now(timezone.utc)
    return fh.home_item(kind, label="Label", title=f"Title of {kind}", detail="", meta="",
                        since=since or now, now=now, actions=[], project=project, **kw)


def units():
    print("== plain words ==")
    ck("plain_wait: the cases of the spec",
       [fh.plain_wait(x) for x in (None, 0, 1, 5, 59, 60, 119, 180, 1440, 2880, 9007, 10080, 30240)]
       == ["just now", "just now", "1 minute", "5 minutes", "59 minutes", "1 hour", "1 hour",
           "3 hours", "1 day", "2 days", "6 days", "1 week", "3 weeks"],
       [fh.plain_wait(x) for x in (None, 0, 1, 5, 59, 60, 119, 180, 1440, 2880, 9007, 10080, 30240)])
    ck("9007 minutes is 6 days", fh.plain_wait(9007) == "6 days")
    ck("plain_wait: the last minute before each unit changes",
       [fh.plain_wait(x) for x in (1439, 10079)] == ["23 hours", "6 days"],
       [fh.plain_wait(x) for x in (1439, 10079)])
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    ck("plain_ago: a span and 'just now' alone",
       fh.plain_ago(now - timedelta(minutes=27), now) == "27 minutes ago"
       and fh.plain_ago(now - timedelta(seconds=17), now) == "just now"
       and fh.plain_ago((now - timedelta(hours=1)).isoformat(), now) == "1 hour ago"
       and fh.plain_ago(None, now) == "")

    print("== home_item ==")
    a = fh.home_item("question", label="Question", title="Which gateway?", detail="d", meta="m",
                     since=now - timedelta(hours=3), now=now,
                     actions=[{"label": "Answer", "primary": True, "do": "answer", "desk": "D1"},
                              {"label": "Open", "href": "/projects/x"}],
                     project={"id": "uc_1", "name": "project-x"}, repo="example-org/docs-site")
    ck("the shape: every field", set(a) == {"id", "kind", "label", "title", "detail", "meta", "project",
                                           "repo", "from", "since", "waited", "actions"}, sorted(a))
    ck("waited in words, since an ISO time", a["waited"] == "3 hours"
       and a["since"] == (now - timedelta(hours=3)).isoformat(), (a["waited"], a["since"]))
    ck("the id is kind, project and a key", a["id"].startswith("question:uc_1:") and a["from"] is None, a["id"])
    ck("the same item has the same id", fh.home_item("question", label="Question",
       title="Which gateway?", detail="x", meta="y", since=now, now=now, actions=[],
       project={"id": "uc_1", "name": "project-x"}, repo="example-org/docs-site")["id"] == a["id"])
    ck("an id given is kept", item("desk", id="desk:mine")["id"] == "desk:mine")
    ck("primary is a bool on every action", [x["primary"] for x in a["actions"]] == [True, False])
    for label, kw in (("an unknown kind", dict(kind="nope")),
                      ("an unknown verb", dict(kind="question", actions=[{"label": "x", "do": "explode"}])),
                      ("an action with both href and do",
                       dict(kind="question", actions=[{"label": "x", "href": "/a", "do": "copy"}]))):
        try:
            fh.home_item(**{"label": "l", "title": "t", "detail": "", "meta": "", "since": now,
                            "now": now, "actions": [], **kw})
            ok = False
        except ValueError:
            ok = True
        ck(f"{label} is refused", ok)

    print("== project_state ==")
    px = {"id": "p1", "status": "active"}
    ho = None
    wq = [{"kind": "question", "project": {"id": "p1", "name": "project-x"}}]
    wp = [{"kind": "prs_to_review", "project": {"id": "p1", "name": "project-x"}}]
    other = [{"kind": "question", "project": {"id": "p2", "name": "project-y"}}]
    merged = [tk(1, "done", "merged"), tk(2, "todo", "merged")]
    mix = [tk(1, "done", "merged"), tk(2, "todo")]
    busy = [tk(1, "done", "merged"), tk(2, "in_progress", "doing")]
    st = fh.project_state
    ck("all merged: ready_to_close, even with something waiting", st(px, merged, ho, wq) == "ready_to_close")
    ck("no ticket started: not_started, even with something waiting",
       st(px, [tk(1), tk(2)], ho, wq) == "not_started" and st(px, [], ho, []) == "not_started")
    ck("merged some, none in progress, a thing waits: waiting_on_you", st(px, mix, ho, wp) == "waiting_on_you")
    ck("merged some, none in progress, nothing waits: building", st(px, mix, ho, []) == "building")
    ck("work in progress and a pull request waits: building", st(px, busy, ho, wp) == "building")
    ck("work in progress and a question waits: waiting_on_you", st(px, busy, ho, wq) == "waiting_on_you")
    ck("work in progress and a permission waits: waiting_on_you",
       st(px, busy, ho, [{"kind": "permission", "project": {"id": "p1", "name": "x"}}]) == "waiting_on_you")
    ck("what waits for another project does not count", st(px, mix, ho, other) == "building"
       and st(px, busy, ho, other) == "building")
    ck("a done project is never ready_to_close", st({"id": "p1", "status": "done"}, merged, ho, []) != "ready_to_close")

    print("== waiting_items ==")
    saved = list(fh.WAITING_BUILDERS)
    ck("no builders, no items", fh.waiting_items(None, None, now) == [])
    t0 = now - timedelta(hours=5)
    fh.WAITING_BUILDERS.append(lambda s, f, n: [fh.home_item("question", label="Q", title="newer",
        detail="", meta="", since=now - timedelta(hours=1), now=n, actions=[]),
        fh.home_item("permission", label="P", title="oldest", detail="", meta="", since=t0, now=n,
                     actions=[])])
    fh.WAITING_BUILDERS.append(lambda s, f, n: [fh.home_item("desk", label="D", title="middle",
        detail="", meta="", since=now - timedelta(hours=2), now=n, actions=[])])
    got = fh.waiting_items(None, None, now)
    ck("builders' lists are joined, oldest first", [i["title"] for i in got] == ["oldest", "middle", "newer"],
       [i["title"] for i in got])
    ck("each says how long it waited", [i["waited"] for i in got] == ["5 hours", "2 hours", "1 hour"])
    fh.WAITING_BUILDERS[:] = saved


def station(name, role, state="idle", project=None, project_name=None, last_at=None, pct=None,
            reason=None, state_at=None):
    return {"name": name, "role": role, "state": state, "project": project,
            "project_name": project_name, "last_at": last_at, "state_at": state_at,
            "state_reason": reason, "context": {"pct": pct} if pct is not None else None}


def members():
    print("== crew_member ==")
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    at = now - timedelta(minutes=8)
    o = fh.crew_member(station("crew-orchestrator", "orchestrator", "idle", last_at=at, pct=82), {}, 0, now)
    ck("an idle orchestrator with an empty desk", o["name"] == "Orchestrator" and o["state"] == "idle"
       and o["doing"] == "Desk empty." and o["since"] == "8 minutes ago"
       and o["memory_pct"] == 82 and o["memory"] == "memory 82% full", o)
    o = fh.crew_member(station("crew-orchestrator", "orchestrator", "idle", last_at=at), {}, 2, now)
    ck("with something on the desk: the role line, no memory reading",
       o["doing"] == "Hands out work and brings you what needs you."
       and o["memory_pct"] is None and o["memory"] is None, o)
    r = fh.crew_member(station("crew-reviewer", "reviewer", "idle", last_at=at), {}, 0, now)
    ck("a reviewer", r["name"] == "Reviewer" and r["doing"] == "Reviews every pull request before you see it.", r)
    b = fh.crew_member(station("b1", "builder", "working", "p1", "project-x", at),
                       {"b1": [tk(1, "done", "merged", title="Old one"),
                               tk(2, "in_progress", "doing", title="Build the footer")]}, 0, now)
    ck("a working builder names its project and its ticket", b["name"] == "Builder, project-x"
       and b["state"] == "working" and b["doing"] == "Working on Build the footer", b)
    w = fh.crew_member(station("b1", "builder", "waiting", "p1", "project-x", at, reason="Waiting for CI"),
                       {}, 0, now)
    ck("a waiting station says why", w["state"] == "waiting" and w["doing"] == "Waiting for CI", w)
    q = fh.crew_member(station("crew-reviewer", "reviewer", "quiet", last_at=now - timedelta(minutes=120)),
                       {}, 0, now)
    ck("a quiet station is 'not heard', since it was last heard", q["state"] == "not heard"
       and q["since"] == "2 hours ago", q)


async def main():
    from tares.daemon import make_app
    app = make_app()
    m.TARESD = mf.TARESD = "http://t"
    mk = lambda timeout=10: httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                              base_url="http://t",
                                              headers={"Authorization": f"Bearer {TOKEN}"})
    m._cx = mf._cx = mk
    store = app.state.store
    fx = factory_api.register(FastAPI(), store, SimpleNamespace(cc_source=lambda: "claude_code"))

    async def get_home(cx):
        fh._cache.clear()
        r = await cx.get("/api/home")
        assert r.status_code == 200, r.text
        return r.json()

    async def ingest(cx, lines):
        r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                          headers={"Content-Type": "application/x-ndjson"})
        assert r.status_code < 300, r.text

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            print("== an empty cell ==")
            h = await get_home(cx)
            ck("no projects, nothing waiting, and a headline that says what to do",
               h["projects"] == [] and h["waiting_for_you"] == [] and h["waiting_total"] == 0
               and h["headline"] == "No projects yet. Start one and hand it to the crew.", h)
            ck("the crew is not running, the plan unknown, no tool connected",
               h["crew"]["running"] is False and h["crew"]["summary"] == "Not running"
               and h["crew"]["members"] == [] and h["plan"] == {"known": False, "limits": [], "share_text": None}
               and [t["connected"] for t in h["tools"]] == [False, False, False], h)
            ck("every key of the shape", set(h) == {"edition", "headline", "waiting_for_you", "waiting_total",
               "projects", "finished_count", "crew", "plan", "tools", "now"}, sorted(h))

            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            ck("the claude_code source is made", r.status_code < 300, r.text)
            h = await get_home(cx)
            ck("a claude_code source counts as Claude Code connected, with no station",
               h["crew"]["members"] == [] and h["tools"][2] == {"name": "Claude Code", "connected": True},
               h["tools"])

            # project-x: building. Two merged, one in progress, one to do; two milestones
            px = json.loads(await m.create_project("project-x", "Ship the docs site"))["id"]
            await mf.write_milestone("M1: first pages", project="project-x")
            await mf.write_milestone("M2: search", project="project-x")
            for t, ms in (("Home page", "M1: first pages"), ("Footer", "M1: first pages"),
                          ("Search box", "M2: search"), ("Search index", "M2: search")):
                await m.write_ticket(t, milestone=ms, project="project-x", repo="example-org/docs-site")
            for n in ("T1", "T2"):
                r = await cx.post(f"/api/projects/{px}/tickets/{n}/stage", json={"stage": "merged"})
                assert r.status_code == 200, r.text
            await cx.post(f"/api/projects/{px}/tickets/T3/assign", json={"holder": "project-x-build-m1"})
            r = await cx.post(f"/api/projects/{px}/tickets/T3/stage",
                              json={"stage": "doing"}, headers={"X-Tares-Station": "project-x-build-m1",
                                                                "X-Tares-Role": "builder"})
            assert r.status_code == 200, r.text
            r = await cx.post(f"/api/projects/{px}/handover",
                              json={"repos": [{"path": "/w/docs-site", "name": "example-org/docs-site"}]})
            assert r.status_code == 200, r.text
            # project-y: a spec doc and tickets, no hand-over
            py = json.loads(await m.create_project("project-y", "Move the blog"))["id"]
            await m.write_doc("spec", "Spec", "Move the blog over.", project="project-y")
            for t in ("Export", "Import"):
                await m.write_ticket(t, project="project-y")
            # project-z: every ticket merged, still open
            pz = json.loads(await m.create_project("project-z", "Tidy the styles"))["id"]
            await m.write_ticket("Tokens", project="project-z")
            r = await cx.post(f"/api/projects/{pz}/tickets/T1/stage", json={"stage": "merged"})
            assert r.status_code == 200, r.text
            # project-d: merged and closed
            pd = json.loads(await m.create_project("project-d", "Old work"))["id"]
            await m.write_ticket("Old", project="project-d")
            await cx.post(f"/api/projects/{pd}/tickets/T1/stage", json={"stage": "merged"})
            r = await cx.post(f"/api/projects/{pd}/finish", json={"words": "done"})
            assert r.status_code == 200, r.text

            await ingest(cx, [
                st_line("o1", "crew-orchestrator", "orchestrator", ago=40),
                state("o1", "idle", 1),
                st_line("r1", "crew-reviewer", "reviewer", ago=40),
                state("r1", "idle", 3),
                st_line("b1", "project-x-build-m1", "builder", "crew-orchestrator", project=px, ago=40),
                state("b1", "working", 2),
                reply("b1", "m1", 20, px), reply("b1", "m2", 19, px), reply("b1", "m3", 18, px),
                reply("b1", "m4", 17, py),
                {"type": "claude_plan", "timestamp": ts(1), "usage_report": {
                    "session": {"total_cost_usd": 0},
                    "rate_limits": {"limits": [
                        {"kind": "session", "group": "session", "percent": 27, "scope": None,
                         "resets_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                         "severity": "normal", "is_active": True},
                        {"kind": "weekly_all", "group": "weekly", "percent": 5, "scope": None,
                         "resets_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                         "severity": "normal", "is_active": False}],
                        "extra_usage": {"is_enabled": False, "monthly_limit": None}}}},
            ])
            r = await cx.post("/api/desk", json={"question": "Which gateway?", "recommendation": "a",
                                                 "why": "w", "project": "project-x"},
                              headers={"X-Tares-Station": "crew-orchestrator", "X-Tares-Role": "orchestrator"})
            ck("a question on the desk", r.status_code < 300, r.text)
            store.upsert_github_credential("cred-a", "tok-a")

            print("== a seeded cell ==")
            h = await get_home(cx)
            by = {p["name"]: p for p in h["projects"]}
            ck("only the open projects are listed, the closed one is counted",
               sorted(by) == ["project-x", "project-y", "project-z"] and h["finished_count"] == 1, sorted(by))
            ck("ordered by state: building, not started, ready to close",
               [p["name"] for p in h["projects"]] == ["project-x", "project-y", "project-z"],
               [p["name"] for p in h["projects"]])
            x = by["project-x"]
            ck("project-x is building, with its counts, repo and builder",
               x["state"] == "building" and x["state_label"] == "Building" and x["goal"] == "Ship the docs site"
               and x["tickets"] == {"merged": 2, "blocked": 0, "total": 4} and x["repos"] == ["docs-site"]
               and x["builders"] == 1 and x["can_close"] is False, x)
            ck("its milestone line and its meta line", x["milestone"] == "Now on M2: search"
               and x["meta"].startswith("1 builder \u00b7 docs-site \u00b7 active ")
               and x["last_activity"] and x["last_activity_at"], (x["milestone"], x["meta"]))
            y = by["project-y"]
            ck("project-y has a spec and tickets but no hand-over: not started",
               y["state"] == "not_started" and y["state_label"] == "Not started" and y["builders"] == 0
               and y["milestone"] is None and y["tickets"] == {"merged": 0, "blocked": 0, "total": 2}
               and y["can_close"] is False, y)
            z = by["project-z"]
            ck("project-z is all merged: ready to close and can be closed",
               z["state"] == "ready_to_close" and z["can_close"] is True and z["milestone"] == "Ready to close"
               and z["tickets"]["merged"] == 1, z)

            c = h["crew"]
            ck("the crew runs, last heard a minute ago", c["running"] is True and c["heard"] == "1 minute ago"
               and c["summary"] == "Running, last heard 1 minute ago" and c["where"] is None and c["heard_at"], c)
            ck("members: orchestrator, reviewer, then the builder of an open project",
               [mm["name"] for mm in c["members"]] == ["Orchestrator", "Reviewer", "Builder, project-x"],
               [mm["name"] for mm in c["members"]])
            mm = {x["role"]: x for x in c["members"]}
            ck("the orchestrator has a question on its desk, so no 'Desk empty.'", c["desk_open"] == 1
               and mm["orchestrator"]["doing"] == "Hands out work and brings you what needs you."
               and mm["orchestrator"]["state"] == "idle", mm["orchestrator"])
            ck("the builder works on its ticket", mm["builder"]["state"] == "working"
               and mm["builder"]["doing"] == "Working on Search box", mm["builder"])
            ck("how far it may go", c["how_far"] == {"autonomy": "L3-review", "releases": True,
                                                     "text": "Up to merge"}, c["how_far"])
            ck("the headline: one ticket in work, nothing waiting",
               h["headline"] == "The crew is working on 1 ticket. Nothing waits for you.", h["headline"])

            lim = {x["kind"]: x for x in h["plan"]["limits"]}
            ck("the plan is known, with its two limits worded",
               h["plan"]["known"] and list(lim) == ["session", "weekly_all"]
               and lim["session"]["label"] == "Current 5 hours" and lim["session"]["pct"] == 27
               and lim["weekly_all"]["label"] == "This week" and lim["weekly_all"]["pct"] == 5, h["plan"])
            import re
            ck("the texts say how much and when it resets",
               re.fullmatch(r"27% used \u00b7 resets (\d\d:\d\d|[A-Z][a-z]{2})", lim["session"]["text"])
               and lim["weekly_all"]["text"] == "5% used \u00b7 resets "
               + (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%a")
               and lim["session"]["resets_at"] and lim["weekly_all"]["resets_at"],
               (lim["session"]["text"], lim["weekly_all"]["text"]))
            ck("who used the plan this week, biggest first",
               h["plan"]["share_text"] == "This week: project-x 75%, project-y 25%", h["plan"]["share_text"])
            ck("the tools: GitHub and Claude Code connected, Linear not",
               h["tools"] == [{"name": "GitHub", "connected": True}, {"name": "Linear", "connected": False},
                              {"name": "Claude Code", "connected": True}], h["tools"])

            ck("no forbidden word or dash anywhere in the body", words.offenders_in(h) == [],
               words.offenders_in(h))
            ck("the page shape is the same on every key", all(set(p) == {
                "id", "name", "goal", "state", "state_label", "milestone", "tickets", "repos", "builders",
                "last_activity_at", "last_activity", "meta", "can_close"} for p in h["projects"]))

            print("== things that wait ==")
            def make(s, f, n):
                return [fh.home_item("question", label="Question", title="Which gateway?", detail="",
                                     meta="", since=n - timedelta(days=2), now=n, actions=[],
                                     project={"id": px, "name": "project-x"}),
                        fh.home_item("prs_to_review", label="Pull requests", title="2 pull requests",
                                     detail="", meta="", since=n - timedelta(minutes=30), now=n,
                                     actions=[{"label": "Review", "primary": True, "href": "/review"}],
                                     project={"id": py, "name": "project-y"})]
            fh.WAITING_BUILDERS.append(make)
            try:
                h = await get_home(cx)
            finally:
                fh.WAITING_BUILDERS.remove(make)
            ck("the oldest first, counted", [i["kind"] for i in h["waiting_for_you"]] == ["question", "prs_to_review"]
               and h["waiting_total"] == 2, h["waiting_for_you"])
            ck("the headline says how many and for how long",
               h["headline"] == "The crew is working on 1 ticket. 2 things wait for you, the oldest for 2 days.",
               h["headline"])
            by = {p["name"]: p for p in h["projects"]}
            ck("a question for project-x puts it first: waiting on you",
               by["project-x"]["state"] == "waiting_on_you" and h["projects"][0]["name"] == "project-x", by["project-x"])
            ck("a waiting pull request does not move a project that was not started",
               by["project-y"]["state"] == "not_started")
            ck("still clean words", words.offenders_in(h) == [], words.offenders_in(h))
            one = lambda s, f, n: make(s, f, n)[:1]
            fh.WAITING_BUILDERS.append(one)
            try:
                h = await get_home(cx)
            finally:
                fh.WAITING_BUILDERS.remove(one)
            ck("one thing: 'waits', for how long", h["headline"] ==
               "The crew is working on 1 ticket. 1 thing waits for you, for 2 days.", h["headline"])
            fresh = lambda s, f, n: [fh.home_item("question", label="Q", title="new", detail="d", meta="m",
                                                  since=n, now=n, actions=[])]
            fh.WAITING_BUILDERS.append(fresh)
            try:
                h = await get_home(cx)
            finally:
                fh.WAITING_BUILDERS.remove(fresh)
            ck("a thing that just arrived does not wait 'for just now'", h["headline"] ==
               "The crew is working on 1 ticket. 1 thing waits for you, for less than a minute.", h["headline"])

            print("== the crew, not running or silent ==")
            now = datetime.now(timezone.utc)
            quiet = [station("crew-orchestrator", "orchestrator", "quiet", last_at=now - timedelta(hours=2))]
            real = fx.stations
            fx.stations = lambda project=None: quiet
            try:
                h2 = fh.home(store, fx, now)
                ck("silent for two hours", h2["crew"]["running"] is True
                   and h2["crew"]["summary"] == "Not heard from in 2 hours"
                   and h2["headline"].startswith("The crew has not been heard from in 2 hours."), h2["headline"])
                quiet[0]["state"] = "ended"
                h2 = fh.home(store, fx, now)
                ck("an ended orchestrator is not running", h2["crew"]["running"] is False
                   and h2["crew"]["summary"] == "Not running" and h2["crew"]["heard"] is None
                   and h2["headline"].startswith("The crew is not running."), h2["crew"])
            finally:
                fx.stations = real
            real_settings = fx.settings
            fx.settings = lambda: {**real_settings(), "autonomy": "L4-ship", "releaser": False}
            try:
                h2 = fh.home(store, fx, now)
                ck("L4-ship without a releaser", h2["crew"]["how_far"] == {
                    "autonomy": "L4-ship", "releases": False,
                    "text": "Merges on its own after review and green CI, no releases"}, h2["crew"]["how_far"])
            finally:
                fx.settings = real_settings

            print("== the route and the cache ==")
            fh._cache.clear()
            r = await cx.get("/api/home")
            ck("GET /api/home on a factory cell returns this body", r.status_code == 200
               and r.json()["edition"] == "factory" and r.json()["projects"][0]["name"] == "project-x"
               and set(r.json()) == set(h), r.text[:200])
            a1 = fh.build(store, fx, SimpleNamespace(state=SimpleNamespace(credential={"name": "key-a"})))
            a2 = fh.build(store, fx, SimpleNamespace(state=SimpleNamespace(credential={"name": "key-a"})))
            b1 = fh.build(store, fx, SimpleNamespace(state=SimpleNamespace(credential={"name": "key-b"})))
            o1, o2 = fh.build(store, fx, None), fh.build(store, fx, None)
            ck("the same credential gets the kept answer, another its own", a1 is a2 and a1 is not b1
               and o1 is o2 and o1 is not a1)
            await m.create_project("project-late", "Made after the answer was kept")
            ck("a change shows after 5 seconds, not before",
               len(fh.build(store, fx, None)["projects"]) == 3 and len(fh.home(store, fx, now)["projects"]) == 4)
            for k, (t, v) in list(fh._cache.items()):
                fh._cache[k] = (t - 6, v)
            ck("and then it shows", len(fh.build(store, fx, None)["projects"]) == 4)

            print("== the cost of a read ==")
            from tares.store import now_utc
            for i in range(50):
                uid = f"uc_load{i:02d}"
                store.create_project(uid, "custom", f"load-{i:02d}", {}, goal="A project for the timing")
                store.set_project_kind(uid, "software_factory")
                for j in range(4):
                    store.create_ticket(uid, "test", f"Task {j}", status="in_progress" if j == 1 else "todo")
            ts_all = [t for p in store.list_projects() for t in store.list_tickets(p["id"])]
            ck("50 more projects and 200 more tickets are seeded", len(ts_all) >= 200, len(ts_all))
            took = []
            for _ in range(20):
                t0 = time.perf_counter()
                fh.home(store, fx, now_utc())
                took.append((time.perf_counter() - t0) * 1000)
            p95 = sorted(took)[18]
            print(f"       home() over 54 projects: p95 {p95:.0f} ms, slowest {max(took):.0f} ms")
            ck("p95 under 300 ms", p95 < 300, f"{p95:.0f} ms")


units()
members()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
