"""What Claude Code sessions spend: tokens per reply, the stations' context windows, the plan's limits.

The Tares side, through the claude_code ingest: a reply's usage counted once however many lines
repeat it, each reply counted for the project it was stamped with (else its station's or its
session's project; the crew's unstamped replies are the crew's), the status line's report on a
station (its context window, the plan's 5-hour and weekly limits), /usage's limits merged with the
newer status line reading, a /context reading and the ask for one, meter lines kept off the
timeline, the fill from replies stored before (and a gap a version that did not count left), and
a project delete.

Run: .venv/bin/python tests/test_claude_usage.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="tares-claude-usage-")
os.environ["TARES_DB"] = os.path.join(_TMP, "api.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(_TMP, "none.yaml")
os.environ["TARES_AUTH_TOKEN"] = TOKEN = "root-token-usage"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"

import httpx

from tares import claude_usage as cu
from tares.pricing import cost_usd

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def ts(minutes_ago: float) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def epoch(minutes_ahead: float) -> int:
    return int((datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead)).timestamp())


def st_line(sid, station, role, project="", ago=60):
    o = {"type": "session_station", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "station": station, "role": role, "parent": "", "factory_station": station}
    if project:
        o["tares_project"] = project
    return o


def reply(sid, mid, ago, out=10, project="", model="claude-opus-5-5", cw=1000, cr=50000, hour=0):
    o = {"type": "assistant", "sessionId": sid, "cwd": "/w/crew", "timestamp": ts(ago),
         "message": {"id": mid, "role": "assistant", "model": model, "content": "ok",
                     "usage": {"input_tokens": 2, "output_tokens": out,
                               "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
                               "cache_creation": {"ephemeral_1h_input_tokens": hour,
                                                  "ephemeral_5m_input_tokens": cw - hour}}}}
    if project:
        o["tares_project"] = project
    return o


STATUS = {"session_id": "b1", "session_name": "shop-build-m1", "version": "2.1.296",
          "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
          "workspace": {"current_dir": "/w/shop"},
          "context_window": {"total_input_tokens": 95300, "total_output_tokens": 800,
                             "context_window_size": 200000, "used_percentage": 48,
                             "remaining_percentage": 52,
                             "current_usage": {"input_tokens": 2, "output_tokens": 40,
                                               "cache_creation_input_tokens": 300,
                                               "cache_read_input_tokens": 94998}},
          "cost": {"total_cost_usd": 1.25},
          "rate_limits": {"five_hour": {"used_percentage": 31, "resets_at": epoch(120)},
                          "seven_day": {"used_percentage": 6, "resets_at": epoch(3000)}}}

CONTEXT = {"model": "claude-opus-5-5", "total_tokens": 95300, "raw_max_tokens": 200000,
           "percentage": 48,
           "categories": [{"name": "System prompt", "tokens": 3000, "kind": "used"},
                          {"name": "Messages", "tokens": 57600, "kind": "used"},
                          {"name": "Free space", "tokens": 71700, "kind": "free"},
                          {"name": "Autocompact buffer", "tokens": 33000, "kind": "buffer"}],
           "mcp_tools": [{"name": "mcp__tares__a", "server_name": "tares", "tokens": 100},
                         {"name": "mcp__tares__b", "server_name": "tares", "tokens": 50},
                         {"name": "mcp__slack__c", "server_name": "slack", "tokens": 400}],
           "memory_files": [{"path": "/w/shop/CLAUDE.md", "type": "Project", "tokens": 955}],
           "skills": [{"name": f"s{i}", "source": "plugin", "tokens": i} for i in range(14)],
           "agents": [{"agent_type": "x", "source": "plugin", "tokens": 337}]}

REPORT = {"session": {"total_cost_usd": 0},
          "rate_limits": {"limits": [
              {"kind": "session", "group": "session", "percent": 27,
               "resets_at": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
               "scope": None, "severity": "normal", "is_active": True},
              {"kind": "weekly_all", "group": "weekly", "percent": 5,
               "resets_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
               "scope": None, "severity": "normal", "is_active": False},
              {"kind": "weekly_scoped", "group": "weekly", "percent": 0,
               "resets_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
               "scope": {"model": {"display_name": "Fable"}, "surface": None},
               "severity": "normal", "is_active": False}],
              "extra_usage": {"is_enabled": False, "monthly_limit": None}}}


def units():
    print("== the reports, kept to what the console shows ==")
    s = cu.compact_status(STATUS)
    ck("status: the window, the model and the plan's limits",
       s["context_window"]["size"] == 200000 and s["context_window"]["used_percentage"] == 48
       and s["model"]["display_name"] == "Opus 5.5" and s["rate_limits"]["five_hour"]["used_percentage"] == 31
       and "workspace" not in s, s)
    ck("status without rate limits (an API key) has none", cu.compact_status({**STATUS, "rate_limits": None})["rate_limits"] is None)
    now = cu.context_now({"status": s, "status_at": None})
    ck("context now: used is the input side of the last reply", now["used"] == 95300 and now["pct"] == 48 and now["size"] == 200000
       and now["compact_at"] is None, now)
    big = cu.compact_status({**STATUS, "autocompact_tokens": 200000,
                             "context_window": {**STATUS["context_window"], "context_window_size": 1000000, "used_percentage": 10}})
    now = cu.context_now({"status": big, "status_at": None})
    ck("a station that compacts at 200k of a 1M window: how near it is", now["pct"] == 10 and now["compact_at"] == 200000
       and now["compact_pct"] == 48, now)
    now = cu.context_now({"status": None, "context": cu.compact_context(CONTEXT), "context_at": "t"})
    ck("no status line yet: the /context reading stands in", now["used"] == 95300 and now["size"] == 200000
       and now["pct"] == 48 and now["at"] == "t", now)
    ck("neither: nothing", cu.context_now({"status": None, "context": None}) is None)
    ck("the Claude Code version comes from the status line", cu.code_version({"status": s}) == "2.1.296")
    ck("no status line yet: no version", cu.code_version({"status": None}) is None and cu.code_version(None) is None)
    c = cu.compact_context(CONTEXT)
    ck("context: MCP tools summed by server, largest first",
       c["mcp_servers"] == [{"name": "slack", "tools": 1, "tokens": 400}, {"name": "tares", "tools": 2, "tokens": 150}], c["mcp_servers"])
    ck("context: the ten largest skills, and how many there are",
       c["skills"]["count"] == 14 and len(c["skills"]["largest"]) == 10 and c["skills"]["largest"][0]["name"] == "s13", c["skills"])
    p = cu.compact_plan(REPORT)
    ck("plan: every limit, named", [x["label"] for x in p["limits"]] ==
       ["Current session (5 hours)", "This week, all models", "This week, Fable"], p)

    print("== the plan now: /usage and the status line ==")
    t0 = datetime.now(timezone.utc) - timedelta(minutes=10)
    merged = cu.plan_now(p, t0, (t0 + timedelta(minutes=5), STATUS["rate_limits"]))
    by = {x["kind"]: x for x in merged["limits"]}
    ck("a newer status line reading wins for its limits", by["session"]["percent"] == 31 and by["session"]["from"] == "status line"
       and by["weekly_all"]["percent"] == 6, by)
    ck("the limit only /usage reads stays", by["weekly_scoped"]["percent"] == 0 and by["weekly_scoped"]["from"] == "usage", by)
    older = cu.plan_now(p, t0, (t0 - timedelta(minutes=5), STATUS["rate_limits"]))
    ck("an older status line reading does not", {x["kind"]: x["percent"] for x in older["limits"]}["session"] == 27, older)
    gone = cu.plan_now(None, None, (t0, {"five_hour": {"used_percentage": 90, "resets_at": epoch(-5)}}))
    ck("a limit past its reset time says so", gone["limits"][0]["reset"] is True, gone)
    ck("nothing read: no plan", cu.plan_now(None, None, None) is None)

    print("== cost ==")
    ck("an hour's cache writes bill at twice the input rate",
       abs(cost_usd("claude-opus-5-5", 0, 0, 1_000_000, 0, 1_000_000) - 8.0) < 1e-9
       and abs(cost_usd("claude-opus-5-5", 0, 0, 1_000_000, 0) - 5.0) < 1e-9)


async def main():
    from tares.daemon import make_app
    from tares.envelope import now_utc
    app = make_app()
    store = app.state.store
    mk = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                   headers={"Authorization": f"Bearer {TOKEN}"})

    async def push(lines):
        async with mk() as cx:
            r = await cx.post("/ingest/claude_code", content="\n".join(json.dumps(x) for x in lines) + "\n",
                              headers={"Content-Type": "application/x-ndjson"})
        return r

    async with app.router.lifespan_context(app):
        async with mk() as cx:
            shop = (await cx.post("/api/projects", json={"template": "custom", "name": "Shop"})).json()["id"]
            other = (await cx.post("/api/projects", json={"template": "custom", "name": "Other"})).json()["id"]
            r = await cx.post("/api/sources", json={"name": "claude_code", "connector": "claude_code",
                                                    "poll": "10s", "config": {"push": True}})
            ck("the claude_code source is made", r.status_code < 300, r.text)

        print("== tokens per reply, for the project they count for ==")
        r = await push([
            st_line("b1", "shop-build-m1", "builder", project="Shop"),
            st_line("o1", "crew-orchestrator", "orchestrator"),
            # one reply on three lines: counted once, with the largest output count
            reply("b1", "m1", 50, out=5), reply("b1", "m1", 50, out=5), reply("b1", "m1", 50, out=40),
            reply("b1", "m2", 40, out=20, hour=1000),
            # the orchestrator: one reply stamped with a project, one not
            reply("o1", "m3", 30, project="Other"), reply("o1", "m4", 20),
            # a session no station plays, linked to Shop the usual way
            {"type": "session_project", "sessionId": "p1", "cwd": "/w/shop", "timestamp": ts(25), "tares_project": "Shop"},
            reply("p1", "m5", 25, model="claude-sonnet-5-5"),
            reply("z1", "m6", 10, model="some-unpriced-model"),
            # Claude Code's own notice ("Autocompact is thrashing"): no model call
            {**reply("z1", "m7", 9, model="<synthetic>"), "message": {**reply("z1", "m7", 9)["message"], "model": "<synthetic>",
                                                                     "usage": {"input_tokens": 0, "output_tokens": 0}}},
        ])
        ck("the push is taken", r.status_code == 202, r.text)
        rows = {x["message"]: x for x in [dict(zip(("session", "message", "output", "cost"), t)) for t in store.con.execute(
            "SELECT session, message, output_tokens, cost_usd FROM session_usage").fetchall()]}
        ck("one row per reply, none for Claude Code's own notices", sorted(rows) == ["m1", "m2", "m3", "m4", "m5", "m6"], rows)
        ck("the largest count wins", rows["m1"]["output"] == 40, rows["m1"])
        ck("priced at write time, an hour's writes at their rate",
           abs(rows["m2"]["cost"] - cost_usd("claude-opus-5-5", 2, 20, 1000, 50000, 1000)) < 1e-12, rows["m2"])
        ck("an unpriced model has no cost", rows["m6"]["cost"] is None, rows["m6"])
        async with mk() as cx:
            u = (await cx.get("/api/claude-code/usage")).json()
        by = {x["name"]: x for x in u["projects"]}
        ck("the builder's replies count for its project, with the session linked to it",
           by["Shop"]["replies"] == 3 and by["Shop"]["sessions"] == 2, by.get("Shop"))
        ck("the orchestrator's stamped reply counts for that project", by["Other"]["replies"] == 1, by.get("Other"))
        ck("its unstamped reply is the crew's", by["The crew (shared)"]["replies"] == 1, by)
        ck("a session nobody claims is other", by["Other sessions"]["replies"] == 1 and by["Other sessions"]["unpriced_replies"] == 1, by)
        ck("shares add up to one", abs(sum(x["share"] for x in u["projects"]) - 1) < 1e-9, u["projects"])
        ck("no plan read yet", u["plan"] is None, u["plan"])

        print("== the status line's report on a station ==")
        status = {"type": "session_status", "sessionId": "b1", "timestamp": ts(1), "status": STATUS}
        await push([status])
        async with mk() as cx:
            crew = (await cx.get("/api/crew")).json()
            b = next(s for s in crew["stations"] if s["name"] == "shop-build-m1")
            u = (await cx.get("/api/claude-code/usage")).json()
        ck("the station shows its context window", b["context"]["pct"] == 48 and b["context"]["size"] == 200000
           and b["context"]["model"] == "Opus 5.5", b["context"])
        ck("and the Claude Code version it runs", b["claude_version"] == "2.1.296", b.get("claude_version"))
        ck("and what its sessions spent", b["usage"]["replies"] == 2 and b["usage"]["output_tokens"] == 60, b["usage"])
        ck("the plan's limits come from the status line",
           [(x["kind"], x["percent"], x["from"]) for x in u["plan"]["limits"]] ==
           [("session", 31, "status line"), ("weekly_all", 6, "status line")], u["plan"])
        ck("the 5-hour window ends at the session limit's reset",
           u["window"]["until"] is not None and len(u["window"]["projects"]) >= 1, u["window"])
        lines = store.key_events("claude_code", "b1", limit=500)
        ck("a meter line is not on the session's timeline",
           not any(x["event_type"] in ("session_status", "session_context") for x in lines), [x["event_type"] for x in lines])
        await push([{**status, "timestamp": ts(30), "status": {**STATUS, "context_window": {**STATUS["context_window"], "used_percentage": 3}}}])
        async with mk() as cx:
            b = next(s for s in (await cx.get("/api/crew")).json()["stations"] if s["name"] == "shop-build-m1")
        ck("an older report does not replace a newer one", b["context"]["pct"] == 48, b["context"])

        print("== /usage's limits ==")
        await push([{"type": "claude_plan", "timestamp": ts(5), "usage_report": REPORT}])
        async with mk() as cx:
            u = (await cx.get("/api/claude-code/usage")).json()
        lim = {x["kind"]: x for x in u["plan"]["limits"]}
        ck("every limit /usage reads", set(lim) == {"session", "weekly_all", "weekly_scoped"}, lim)
        ck("the newer status line still wins for the session", lim["session"]["percent"] == 31, lim["session"])
        ck("the Fable week from /usage", lim["weekly_scoped"]["label"] == "This week, Fable" and lim["weekly_scoped"]["percent"] == 0, lim)
        ck("extra usage is said", u["plan"]["extra_usage"]["is_enabled"] is False, u["plan"])

        print("== a /context reading, asked from the console ==")
        async with mk() as cx:
            r = await cx.post("/api/crew/shop-build-m1/context")
            ck("asking for a reading", r.status_code == 200 and r.json()["session"] == "b1", r.text)
            asks = (await cx.get("/api/crew/context/asks")).json()["asks"]
            ck("the watch sees the ask", asks == [{"station": "shop-build-m1", "session": "b1"}], asks)
            ck("an unknown station is a 404", (await cx.post("/api/crew/nobody/context")).status_code == 404)
        await push([{"type": "session_context", "sessionId": "b1", "timestamp": ts(0), "context_usage": CONTEXT}])
        async with mk() as cx:
            asks = (await cx.get("/api/crew/context/asks")).json()["asks"]
            d = (await cx.get("/api/crew/shop-build-m1")).json()
        ck("a reading answers the ask", asks == [], asks)
        ck("the station carries the reading", d["context_detail"]["total_tokens"] == 95300
           and d["context_detail"]["categories"][1] == {"name": "Messages", "tokens": 57600, "kind": "used"}
           and d["context_at"] and d["context_asked_at"], d.get("context_detail"))

        print("== the replies stored before ==")
        with store._lock:
            store.con.execute("DELETE FROM session_usage")
            store._claude_usage_fill()
            n = store.con.execute("SELECT count(*), max(output_tokens) FILTER (WHERE message = 'm1'), "
                                  "any_value(project) FILTER (WHERE message = 'm3'), "
                                  "count(cost_usd), any_value(cost_usd) FILTER (WHERE message = 'm2') "
                                  "FROM session_usage").fetchone()
        ck("filled from the stored lines, one row a reply", n[0] == 6 and n[1] == 40, n)
        ck("with the stamped project, and priced", n[2] == other and n[3] == 5, n)
        ck("an hour's cache writes priced at their rate",
           abs(n[4] - cost_usd("claude-opus-5-5", 2, 20, 1000, 50000, 1000)) < 1e-12, n)
        with store._lock:
            store.con.execute("DELETE FROM session_usage WHERE message = 'm1'")
            store._claude_usage_fill()
            again = store.con.execute("SELECT count(*) FROM session_usage").fetchone()[0]
        ck("a gap left by a version that did not count is filled at the next start", again == 6, again)
        from datetime import datetime, timezone
        store.record_reply_usage("b1", "mx", datetime.now(timezone.utc), "claude-opus-5-6", None, None,
                                 {"input_tokens": 5, "output_tokens": 5})
        with store._lock:
            store._claude_usage_fill()
            unlisted = store.con.execute(
                "SELECT cost_usd FROM session_usage WHERE message = 'mx'").fetchone()[0]
            store.con.execute("DELETE FROM session_usage WHERE message = 'mx'")
        ck("a model version the table does not list stays unpriced through the fill", unlisted is None, unlisted)
        # unpriced rows, as a version that did not price them leaves them; the fill prices what the table lists
        for msg, model in (("md", "claude-haiku-4-5-20251001"), ("mr", "claude-opus-4-10")):
            store.record_reply_usage("b1", msg, datetime.now(timezone.utc), model, None, None,
                                     {"input_tokens": 1_000_000, "output_tokens": 0})
        with store._lock:
            store.con.execute("UPDATE session_usage SET cost_usd = NULL WHERE message IN ('md', 'mr')")
            store._claude_usage_fill()
            got = dict(store.con.execute(
                "SELECT message, cost_usd FROM session_usage WHERE message IN ('md', 'mr')").fetchall())
            store.con.execute("DELETE FROM session_usage WHERE message IN ('md', 'mr')")
        ck("a dated snapshot is priced through the fill", got["md"] is not None and abs(got["md"] - 1.0) < 1e-9, got)
        ck("a version that runs on from a listed prefix stays unpriced through the fill", got["mr"] is None, got)

        print("== a project delete ==")
        async with mk() as cx:
            await cx.delete(f"/api/projects/{other}")
            u = (await cx.get("/api/claude-code/usage")).json()
        names = {x["name"] for x in u["projects"]}
        ck("its replies stay, counted for no project", "Other" not in names and "a deleted project" not in names
           and sum(x["replies"] for x in u["projects"]) == 6, u["projects"])


units()
asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
