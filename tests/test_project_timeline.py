"""Run lineage and the project timeline (TR-330, TR-331).

Every run records what woke it (trigger, schedule, manual, rerun, bootstrap), the run it repeats
and its project; a firing records its project and, when a finding tripped it, the run that wrote
the finding. GET /api/projects/{uid}/timeline puts that together as threads: a watcher's firing,
its run, the firing its finding tripped and the root-cause run that one woke read as ONE thread.

The model loop is replaced by a stand-in that concludes at once, so the whole path (dispatch,
run record, finding event, the findings trigger, the timeline) runs for real without a provider.

Run: .venv/bin/python tests/test_project_timeline.py
"""
import asyncio
import json
import os

DB = "/tmp/tares-timeline.duckdb"
OLD_DB = "/tmp/tares-timeline-old.duckdb"
os.environ["TARES_DB"] = DB
os.environ["TARES_CATALOG"] = "/tmp/tares-timeline.catalog.yaml"
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
# enable checks that a key resolves; the stand-in loop below never calls a model
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"
for _p in (DB, DB + ".wal", OLD_DB, OLD_DB + ".wal", os.environ["TARES_CATALOG"]):
    if os.path.exists(_p):
        os.remove(_p)

import duckdb
import httpx

from tares.builtin_agents import AgentRunner
from tares.config import FINDINGS_SOURCE
from tares.store import Store

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


async def fake_run(self, agent, trigger_name, key, payload, run_id, dispatch_id=None):
    """Conclude at once with a finding, through the real _record (finding event, lineage)."""
    text = f"{agent['name']} looked at {key}"
    verdict = "investigate" if agent["name"] == "watcher" else "done"
    await self._record(agent, trigger_name, key, text, verdict=verdict, run_id=run_id,
                       dispatch_id=dispatch_id)
    self.store.finish_agent_run(run_id, "ok", rounds=1, finding=text, outcome="finding",
                                verdict=verdict)
    return "ok", None


AgentRunner._run = fake_run

SOURCE = {"name": "evt", "connector": "webhook", "poll": "5s",
          "config": {"event_type": "log", "text_template": "{msg}",
                     "labels": [{"name": "service", "field": "service", "primary": True}]}}


def cond(pred="> 0"):
    return {"aggregate": "count", "predicate": pred, "window": "5m"}


async def wait_for(fn, tries=100):
    for _ in range(tries):
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.05)
    return fn()


def all_runs(thread):
    stack, out = list(thread["runs"]), []
    while stack:
        r = stack.pop()
        out.append(r)
        stack += r["children"]
        for t in r["firings"]:
            stack += t["runs"]
    return out


async def main():
    from tares.daemon import make_app
    from tares import schedule
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        store = app.state.store
        runtime = app.state.runtime
        runner = app.state.agents
        runner.attach_loop()

        async def mk(kind, body):
            r = await cx.post(f"/api/{kind}", json=body)
            assert r.status_code == 201, (kind, r.text)
            return r.json()

        a = (await mk("projects", {"template": "custom", "name": "Alpha", "objects": []}))["id"]
        b = (await mk("projects", {"template": "custom", "name": "Beta", "objects": []}))["id"]
        await mk("sources", {**SOURCE, "project": a})
        store.upsert_catalog_source(FINDINGS_SOURCE, "finding", "finding", "5s", {})
        runtime.reload_catalog()
        # Alpha: a watcher on the logs, a root-cause agent woken by the watcher's findings (a
        # findings trigger, not a handoff), and a schedule
        await mk("triggers", {"name": "watch", "project": a, "sources": ["evt"],
                              "key_field": "service", "condition": cond(), "cooldown": "1h"})
        await mk("triggers", {"name": "rca", "project": a, "sources": [FINDINGS_SOURCE],
                              "key_field": "service", "condition": cond(), "cooldown": "1h",
                              "filters": [{"field": "agent", "op": "eq", "value": "watcher"}]})
        await mk("triggers", {"name": "tick", "project": a, "sources": ["evt"],
                              "condition": {"every": "10m", "summary_by": ["service"]}})
        # Beta reads the same logs and Alpha's watcher findings: none of it may show in Alpha
        await mk("triggers", {"name": "beta_watch", "project": b, "sources": ["evt"],
                              "key_field": "service", "condition": cond(), "cooldown": "1h"})
        await mk("triggers", {"name": "beta_rca", "project": b, "sources": [FINDINGS_SOURCE],
                              "key_field": "service", "condition": cond(), "cooldown": "1h",
                              "filters": [{"field": "agent", "op": "eq", "value": "watcher"}]})
        for name, trig, proj in (("watcher", "watch", a), ("rooter", "rca", a),
                                 ("ticker", "tick", a), ("beta_agent", "beta_watch", b)):
            await mk("agents/builtin", {"name": name, "trigger": trig, "prompt": "look",
                                        "project": proj})
            r = await cx.post(f"/api/agents/builtin/{name}/enable")
            assert r.status_code == 200, r.text

        print("== trigger wake: watcher, its finding, the root-cause run ==")
        await cx.post("/ingest/evt", json={"service": "checkout", "msg": "500"})
        rooted = await wait_for(lambda: [r for r in store.list_agent_runs("rooter")
                                         if r["status"] == "ok"])
        w = next((r for r in store.list_agent_runs("watcher")), None)
        ck("watcher ran, woken by the trigger, in Alpha",
           w is not None and w["woken_by"] == "trigger" and w["project"] == a
           and w["parent_run_id"] is None and w["dispatch_id"], str(w))
        ck("root-cause agent ran on the watcher's finding", len(rooted) == 1, str(rooted))
        fev = store.con.execute(
            "SELECT payload, labels FROM events WHERE source = ? AND "
            "json_extract_string(payload, '$.agent') = 'watcher'", [FINDINGS_SOURCE]).fetchone()
        pay, lab = json.loads(fev[0]), json.loads(fev[1])
        ck("the finding event carries run_id and dispatch_id in payload and labels",
           pay.get("run_id") == w["id"] and lab.get("run_id") == w["id"]
           and pay.get("dispatch_id") == w["dispatch_id"] == lab.get("dispatch_id"), str((pay, lab)))
        ck("run_id is not counted as an entity axis",
           not store.con.execute("SELECT 1 FROM entity_counts WHERE label IN ('run_id', "
                                 "'dispatch_id')").fetchone())
        rca_fire = store.con.execute("SELECT project, parent_run_id FROM dispatch_log "
                                     "WHERE trigger = 'rca'").fetchone()
        ck("the rca firing records its project and the run that tripped it",
           rca_fire == (a, w["id"]), str(rca_fire))
        bfire = await wait_for(lambda: store.con.execute(
            "SELECT project, parent_run_id FROM dispatch_log WHERE trigger = 'beta_rca'").fetchone())
        ck("Beta's findings firing is Beta's", bfire and bfire[0] == b, str(bfire))

        tl = (await cx.get(f"/api/projects/{a}/timeline")).json()
        wt = [t for t in tl["threads"] if t["kind"] == "firing" and t["trigger"] == "watch"]
        ck("one thread for the watch firing", len(wt) == 1, str(tl)[:400])
        t = wt[0]
        ck("thread shape: fired, runs, deliveries",
           t["fired"]["trigger"] == "watch" and t["fired"]["kind"] == "condition"
           and t["fired"]["dispatch_id"] == t["id"] and len(t["fired"]["payload_excerpt"]) <= 600
           and t["entity"] == "checkout" and [x["agent"] for x in t["runs"]] == ["watcher"]
           and any(d["kind"] == "tares" and d["target"] == "watcher" for d in t["deliveries"]),
           json.dumps(t, default=str)[:600])
        run = t["runs"][0]
        ck("run item carries the runs-table fields and lineage",
           all(k in run for k in ("id", "agent", "status", "outcome", "verdict", "key", "model",
                                  "rounds", "tool_calls", "cost_usd", "duration_ms", "finding",
                                  "results", "started_at", "woken_by", "children")),
           str(sorted(run)))
        sub = run["firings"]
        ck("the rca firing and its run nest under the watcher's run",
           len(sub) == 1 and sub[0]["trigger"] == "rca"
           and [x["agent"] for x in sub[0]["runs"]] == ["rooter"], json.dumps(sub, default=str)[:400])
        ck("the rca firing is not a thread of its own",
           not any(x["trigger"] == "rca" for x in tl["threads"]))

        tb = (await cx.get(f"/api/projects/{b}/timeline")).json()
        b_triggers = {x["trigger"] for x in tb["threads"]}
        ck("Beta sees its own firings, the findings one as a thread of its own",
           b_triggers == {"beta_watch", "beta_rca"}, str(b_triggers))
        a_all = json.dumps(tl, default=str)
        ck("no firing or run of Beta appears in Alpha",
           "beta_watch" not in a_all and "beta_rca" not in a_all and "beta_agent" not in a_all)
        ck("no firing or run of Alpha appears in Beta",
           not any(r["agent"] in ("watcher", "rooter", "ticker") for x in tb["threads"]
                   for r in all_runs(x))
           and not {"watch", "rca", "tick"} & {x["trigger"] for x in tb["threads"]})

        print("== schedule wake ==")
        await schedule.tick(store, runtime.catalog, runtime.dispatcher)
        tick_run = await wait_for(lambda: [r for r in store.list_agent_runs("ticker")
                                           if r["status"] == "ok"])
        ck("the schedule's run is woken_by schedule",
           tick_run and tick_run[0]["woken_by"] == "schedule" and tick_run[0]["project"] == a,
           str(tick_run))
        tl = (await cx.get(f"/api/projects/{a}/timeline?trigger=tick")).json()
        ck("the schedule firing's kind is schedule",
           tl["threads"] and tl["threads"][0]["fired"]["kind"] == "schedule", str(tl)[:300])

        print("== manual, rerun, bootstrap ==")
        manual = runner.run_now("watcher", "watch", "payments", "a note")
        await wait_for(lambda: (store.get_agent_run(manual) or {}).get("status") == "ok")
        m = store.get_agent_run(manual)
        ck("run_now is manual, no parent, in Alpha",
           m["woken_by"] == "manual" and m["parent_run_id"] is None and m["project"] == a, str(m))
        r = await cx.post(f"/api/agents/builtin/watcher/runs/{manual}/rerun")
        rerun = r.json().get("run_id")
        await wait_for(lambda: (store.get_agent_run(rerun) or {}).get("status") == "ok")
        rr = store.get_agent_run(rerun)
        ck("the rerun route records rerun and the run it repeats",
           rr["woken_by"] == "rerun" and rr["parent_run_id"] == manual, str(rr))
        r = await cx.post(f"/api/agents/builtin/watcher/runs/{w['id']}/rerun")
        rerun2 = r.json().get("run_id")
        await wait_for(lambda: (store.get_agent_run(rerun2) or {}).get("status") == "ok")
        runner.bootstrap("watcher", "watch", ["checkout"], delay_s=0)
        boot = await wait_for(lambda: [x for x in store.list_agent_runs("watcher")
                                       if x["woken_by"] == "bootstrap" and x["status"] == "ok"])
        ck("bootstrap runs are woken_by bootstrap", len(boot) == 1 and boot[0]["project"] == a,
           str(boot))

        tl = (await cx.get(f"/api/projects/{a}/timeline")).json()
        runs_threads = {x["id"]: x for x in tl["threads"] if x["kind"] == "run"}
        ck("a manual run is a thread of kind run",
           manual in runs_threads and runs_threads[manual]["runs"][0]["id"] == manual)
        kids = runs_threads.get(manual, {}).get("runs", [{}])[0].get("children", [])
        ck("its rerun nests under it, not as a thread",
           [k["id"] for k in kids] == [rerun] and rerun not in runs_threads, str(kids)[:300])
        wt = next(x for x in tl["threads"] if x["trigger"] == "watch" and x["kind"] == "firing")
        ck("a rerun of a firing's run nests under that run",
           [k["id"] for k in wt["runs"][0]["children"]] == [rerun2], str(wt["runs"][0]["children"])[:300])
        ck("the bootstrap run is a thread of kind run", boot[0]["id"] in runs_threads)
        ats = [x["at"] for x in tl["threads"]]
        ck("threads newest first", ats == sorted(ats, reverse=True), str(ats))

        print("== filters ==")
        q = lambda s: cx.get(f"/api/projects/{a}/timeline?{s}")
        # the manual run on payments wrote a watcher finding too, which tripped rca for payments:
        # the rooter sits under the watch firing's run and under the manual run
        f1 = (await q("agent=rooter")).json()["threads"]
        ck("agent= matches a nested run",
           {x["id"] for x in f1} == {wt["id"], manual}, str([x["id"] for x in f1]))
        mt = next(x for x in f1 if x["id"] == manual)
        ck("a firing tripped by a manual run's finding nests under that run",
           [f["trigger"] for f in mt["runs"][0]["firings"]] == ["rca"], str(mt)[:300])
        f2 = (await q("trigger=rca")).json()["threads"]
        ck("trigger= matches a nested firing", {x["id"] for x in f2} == {wt["id"], manual})
        f3 = (await q("entity=payments")).json()["threads"]
        ck("entity= keeps the payments threads only",
           {x["id"] for x in f3} == {manual}, str([x["id"] for x in f3]))
        f4 = (await q("outcome=finding&agent=ticker")).json()["threads"]
        ck("filters combine", len(f4) == 1 and f4[0]["trigger"] == "tick")
        f5 = (await q("outcome=failed")).json()["threads"]
        ck("outcome=failed: none failed", f5 == [], str(f5)[:200])
        ck("a bad outcome is refused", (await q("outcome=nope")).status_code == 400)
        ck("an unknown project is 404",
           (await cx.get("/api/projects/uc_nope/timeline")).status_code == 404)
        ck("a bad before is refused", (await q("before=yesterday")).status_code == 400)

        print("== paging past 100 firings ==")
        base = len(tl["threads"])
        for i in range(130):
            store.log_dispatch(f"pa{i:03d}", "watch", f"k{i}", "x", 0, 0, "p", project=a)
            store.log_dispatch(f"pb{i:03d}", "beta_watch", f"k{i}", "x", 0, 0, "p", project=b)
        seen, before, pages = [], "", 0
        while True:
            params = {"limit": 50, **({"before": before} if before else {})}
            page = (await cx.get(f"/api/projects/{a}/timeline", params=params)).json()
            seen += [x["id"] for x in page["threads"]]
            pages += 1
            if not page["next_before"] or pages > 10:
                break
            before = page["next_before"]
        ck("paging with before reaches every Alpha thread, none twice",
           len(seen) == base + 130 and len(set(seen)) == len(seen)
           and not any(s.startswith("pb") for s in seen), f"{len(seen)} vs {base + 130}")
        f6 = (await q("outcome=none&limit=200")).json()
        ck("outcome=none finds the firings that woke nobody, across more than 100",
           len([x for x in f6["threads"] if x["id"].startswith("pa")]) == 130,
           str(len(f6["threads"])))

    print("== upgrade of an old database ==")
    old = Store(OLD_DB)
    old.upsert_catalog_source("evt", "webhook", "webhook", "5s", {})
    old.upsert_catalog_trigger("t", ["evt"], cond(), {}, "5m")
    old.upsert_catalog_agent("ag", "t", "look")
    old.normalize_projects()
    owner = old.get_catalog_agent("ag")["owned_by"]
    old.con.close()
    con = duckdb.connect(OLD_DB)
    # an old file has only the agent index; the release before lineage had no others
    for ix in ("ix_agent_runs_agent", "ix_agent_runs_dispatch", "ix_agent_runs_parent", "ix_agent_runs_project",
               "ix_dispatch_log_project", "ix_dispatch_log_parent"):
        con.execute(f"DROP INDEX IF EXISTS {ix}")
    for col in ("woken_by", "parent_run_id", "project"):
        con.execute(f"ALTER TABLE agent_runs DROP COLUMN {col}")
    for col in ("project", "parent_run_id", "practice"):
        con.execute(f"ALTER TABLE dispatch_log DROP COLUMN {col}")
    con.execute("DELETE FROM settings WHERE key = 'lineage_backfilled'")   # set by a release with lineage
    con.execute("INSERT INTO agent_runs (id, agent, trigger, dispatch_id, key_value, status, "
                "started_at) VALUES ('r_old', 'ag', 't', 'd_old', 'x', 'ok', now()), "
                "('r_gone', 'deleted-agent', 't', '', 'x', 'ok', now())")
    con.execute("INSERT INTO dispatch_log VALUES ('d_old', 't', 'x', 'k', now(), 1, 1, 'p')")
    con.close()
    for attempt in (1, 2):
        st = Store(OLD_DB)
        r = st.get_agent_run("r_old")
        ck(f"open {attempt}: an old run keeps woken_by unknown, gets its agent's project",
           r and r["woken_by"] is None and r["project"] == owner, str(r))
        ck(f"open {attempt}: a run of a deleted agent stays unplaced",
           st.get_agent_run("r_gone")["project"] is None)
        d = st.con.execute("SELECT project FROM dispatch_log WHERE dispatch_id = 'd_old'").fetchone()
        ck(f"open {attempt}: an old firing gets its trigger's project", d == (owner,), str(d))
        tl = __import__("tares.timeline", fromlist=["x"]).project_timeline(st, owner)
        ck(f"open {attempt}: the old firing and its run form one thread",
           len(tl["threads"]) == 1 and tl["threads"][0]["runs"][0]["id"] == "r_old", str(tl)[:300])
        st.con.close()

    print(f"\n{P} passed, {F} failed")
    return 1 if F else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
