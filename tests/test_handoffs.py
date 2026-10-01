"""Handoffs (TR-334): an agent names, per verdict, the agent that takes over when it concludes.

Covers validation through the API (self, unknown agent, bad verdict, more than
ten, bad cooldown), the runtime (a matching verdict starts the target on the concluded entity with
woken_by=handoff and the parent run, case-insensitively; other verdicts and no_op do not), the
input the target is handed, the depth cap, the cooldown note on the parent run, the daily cap and
an agent already running recorded as capped runs, the timeline nesting, export and import, and
deleting the target (its handoffs go with it).

The model loop is a stand-in that concludes from a script per agent, so the real run path
(`_run_traced`, the finding event, the run record) and the real handoff step run without a model.

Run: .venv/bin/python tests/test_handoffs.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
# a provider must resolve for a run to start; the stand-in loop never calls it
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx
import yaml

import tares.builtin_agents as ba
from tares.builtin_agents import AgentRunner

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


# agent name -> (outcome, verdict, key or None, label or None); what its next run concludes
SCRIPT: dict = {}
SEEN: dict = {}     # agent name -> [(key, payload, handoff)]


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    SEEN.setdefault(agent["name"], []).append((key, payload, handoff))
    outcome, verdict, ckey, label = SCRIPT.get(agent["name"], ("finding", "done", None, None))
    summary = f"{agent['name']} concluded {verdict} on {ckey or key}"
    concluded.update({"outcome": outcome, "summary": summary, "verdict": verdict,
                      "key": ckey, "label": label, "produced": []})
    return summary, 1, 1, [], False, ""


AgentRunner._loop = fake_loop

SOURCE = {"name": "evt", "connector": "webhook", "poll": "5s",
          "config": {"event_type": "log", "text_template": "{msg}",
                     "labels": [{"name": "service", "field": "service", "primary": True}]}}


async def wait_for(fn, tries=200):
    for _ in range(tries):
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.03)
    return fn()


def runs(store, agent):
    return list(reversed(store.list_agent_runs(agent, limit=100)))


def settled(store, agent, n):
    """`agent` has at least n runs and none still running."""
    rs = runs(store, agent)
    return rs if len(rs) >= n and all(r["status"] != "running" for r in rs) else None


def walk(item):
    stack, out = [item], []
    while stack:
        r = stack.pop()
        out.append(r)
        stack += r["children"]
        for t in r["firings"]:
            stack += t["runs"]
    return out


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        store = app.state.store
        runner = app.state.agents
        runner.attach_loop()

        async def mk(kind, body, status=201):
            r = await cx.post(f"/api/{kind}", json=body)
            assert r.status_code == status, (kind, r.status_code, r.text)
            return r.json()

        a = (await mk("projects", {"template": "custom", "name": "Alpha", "objects": []}))["id"]
        b = (await mk("projects", {"template": "custom", "name": "Beta", "objects": []}))["id"]
        await mk("sources", {**SOURCE, "project": a})
        await mk("triggers", {"name": "watch", "project": a, "sources": ["evt"],
                              "key_field": "service", "cooldown": "1h",
                              "condition": {"aggregate": "count", "predicate": "> 0",
                                            "window": "5m"}})
        await mk("triggers", {"name": "beta_watch", "project": b, "sources": ["evt"],
                              "key_field": "service", "cooldown": "1h",
                              "condition": {"aggregate": "count", "predicate": "> 0",
                                            "window": "5m"}})
        for name in ("rca", "rca2", "capt", "c1", "c2", "c3", "c4", "c5"):
            await mk("agents/builtin", {"name": name, "trigger": "watch", "prompt": "look",
                                        "project": a})
        await mk("agents/builtin", {"name": "other", "trigger": "beta_watch", "prompt": "look",
                                    "project": b})

        print("== validation ==")

        async def bad(handoffs, word, name="triage"):
            r = await cx.post("/api/agents/builtin", json={
                "name": name, "trigger": "watch", "prompt": "look", "project": a,
                "handoffs": handoffs})
            return r.status_code == 400 and word in r.text, f"{r.status_code} {r.text[:200]}"

        ok_, d = await bad([{"verdict": "investigate", "agent": "triage"}], "itself")
        ck("an agent cannot hand off to itself", ok_, d)
        # parts are shared (P-TR-216): any agent on the cell can take a handoff
        r = await cx.post("/api/agents/builtin", json={
            "name": "triage", "trigger": "watch", "prompt": "look", "project": a,
            "handoffs": [{"verdict": "investigate", "agent": "other"}]})
        ck("a handoff to an agent another project made is allowed", r.status_code == 201, r.text)
        await cx.delete("/api/agents/builtin/triage")
        ok_, d = await bad([{"verdict": "investigate", "agent": "ghost"}], "unknown agent")
        ck("a handoff to an unknown agent is refused", ok_, d)
        ok_, d = await bad([{"verdict": "look closer", "agent": "rca"}], "one word")
        ck("a verdict of two words is refused", ok_, d)
        ok_, d = await bad([{"verdict": "", "agent": "rca"}], "one word")
        ck("an empty verdict is refused", ok_, d)
        ok_, d = await bad([{"verdict": f"v{i}", "agent": "rca"} for i in range(11)], "at most 10")
        ck("more than ten handoffs are refused", ok_, d)
        ok_, d = await bad([{"verdict": "x", "agent": "rca", "cooldown": "soon"}], "cooldown")
        ck("a cooldown that is not a duration is refused", ok_, d)
        ck("nothing was created by the refused requests",
           store.get_catalog_agent("triage") is None)

        await mk("agents/builtin", {"name": "triage", "trigger": "watch", "prompt": "look",
                                    "project": a, "handoffs": [
                                        {"verdict": "Investigate", "agent": "rca"},
                                        {"verdict": "escalate", "agent": "rca2", "cooldown": "2h"}]})
        tri = store.get_catalog_agent("triage")
        ck("stored lowercased, with the default cooldown of 30m",
           tri["handoffs"] == [{"verdict": "investigate", "agent": "rca", "cooldown": "30m"},
                               {"verdict": "escalate", "agent": "rca2", "cooldown": "2h"}],
           str(tri["handoffs"]))
        listed = next(x for x in (await cx.get("/api/agents/builtin")).json()["agents"]
                      if x["name"] == "triage")
        ck("the agents list carries the handoffs", listed["handoffs"] == tri["handoffs"])
        r = await cx.put("/api/agents/builtin/triage", json={"trigger": "watch", "prompt": "look2"})
        ck("an update without handoffs keeps them",
           r.status_code == 200 and store.get_catalog_agent("triage")["handoffs"] == tri["handoffs"],
           r.text)
        r = await cx.put("/api/agents/builtin/other", json={
            "trigger": "beta_watch", "prompt": "look", "handoffs": [{"verdict": "x", "agent": "rca"}]})
        ck("an update to an agent of another project is that project's wiring",
           r.status_code == 200 and [(h["verdict"], h["agent"]) for h in
                                     store.list_handoffs(project=b, from_agent="other")] == [("x", "rca")],
           r.text)
        # only triage wakes on the trigger; the others run when handed off (or run by hand)
        r = await cx.post("/api/agents/builtin/triage/enable")
        assert r.status_code == 200, r.text

        print("== a matching verdict starts the target ==")
        SCRIPT["triage"] = ("finding", "INVESTIGATE", "checkout-api", "service")
        await cx.post("/ingest/evt", json={"service": "checkout", "msg": "500"})
        rr = await wait_for(lambda: settled(store, "rca", 1))
        tr = runs(store, "triage")[0]
        ck("triage ran on the trigger", tr["woken_by"] == "trigger" and tr["status"] == "ok", str(tr))
        ck("rca was started once", rr is not None and len(rr) == 1, str(rr))
        rc = rr[0] if rr else {}
        ck("woken_by handoff, parent the triage run, in the project",
           rc.get("woken_by") == "handoff" and rc.get("parent_run_id") == tr["id"]
           and rc.get("project") == a and not rc.get("dispatch_id"), str(rc))
        ck("on the concluded entity, not the woken one", rc.get("key") == "checkout-api", str(rc))
        ck("the target's run ended ok", rc.get("status") == "ok", str(rc))
        key, payload, handoff = SEEN["rca"][0]
        ck("the target is told it was handed off", handoff is True)
        ck("its input names the handing agent, verdict and entity, then the finding",
           payload.startswith('Handed off by triage with verdict "investigate" on '
                              "service=checkout-api.")
           and "triage concluded INVESTIGATE on checkout-api" in payload, payload)
        ck("a different verdict's agent was not started", not runs(store, "rca2"))

        print("== other verdicts and no_op do not hand off ==")
        SCRIPT["triage"] = ("finding", "fine", "billing", None)
        runner.run_now("triage", "watch", "billing", "")
        await wait_for(lambda: settled(store, "triage", 2))
        SCRIPT["triage"] = ("no_op", "investigate", "billing", None)
        runner.run_now("triage", "watch", "billing", "")
        await wait_for(lambda: settled(store, "triage", 3))
        await asyncio.sleep(0.2)
        ck("no rca run for a non-matching verdict or a no_op",
           [r["key"] for r in runs(store, "rca")] == ["checkout-api"],
           str([(r["key"], r["woken_by"]) for r in runs(store, "rca")]))

        print("== cooldown per from, to and entity ==")
        SCRIPT["triage"] = ("finding", "investigate", "checkout-api", "service")
        runner.run_now("triage", "watch", "checkout", "")
        t4 = await wait_for(lambda: settled(store, "triage", 4))
        await asyncio.sleep(0.2)
        ck("inside the cooldown: no new rca run", len(runs(store, "rca")) == 1,
           str(runs(store, "rca")))
        last = runs(store, "triage")[-1]
        ck("the skip is a note on the parent run",
           {"kind": "custom", "label": "handoff to rca skipped: cooldown"} in (last["results"] or []),
           str(last["results"]))
        ck("the cooldown is kept in trigger_state",
           store.last_fired(ba.handoff_state("triage", "rca"), "checkout-api") is not None)
        SCRIPT["triage"] = ("finding", "investigate", "payments", "service")
        runner.run_now("triage", "watch", "payments", "")
        await wait_for(lambda: settled(store, "rca", 2))
        ck("another entity is not in that cooldown",
           [r["key"] for r in runs(store, "rca")] == ["checkout-api", "payments"],
           str([r["key"] for r in runs(store, "rca")]))

        print("== depth cap ==")
        for n, nxt in (("c1", "c2"), ("c2", "c3"), ("c3", "c4"), ("c4", "c5"), ("c5", "c1")):
            r = await cx.put(f"/api/agents/builtin/{n}", json={
                "trigger": "watch", "prompt": "look",
                "handoffs": [{"verdict": "next", "agent": nxt, "cooldown": "0s"}]})
            assert r.status_code == 200, r.text
            SCRIPT[n] = ("finding", "next", None, None)
        root = runner.run_now("c1", "watch", "chain", "")
        c5 = await wait_for(lambda: settled(store, "c5", 1))
        await asyncio.sleep(0.2)
        chain = {n: runs(store, n) for n in ("c1", "c2", "c3", "c4", "c5")}
        ck("c2, c3 and c4 ran (three handoffs)",
           all(len(chain[n]) == 1 and chain[n][0]["status"] == "ok" for n in ("c2", "c3", "c4")),
           str({n: [(r["status"], r["woken_by"]) for r in v] for n, v in chain.items()}))
        cap = (c5 or [{}])[0]
        ck("the fourth handoff is a capped run with the reason",
           cap.get("status") == "capped" and cap.get("error") == "handoff chain stopped at depth 3"
           and cap.get("woken_by") == "handoff"
           and cap.get("parent_run_id") == chain["c4"][0]["id"], str(cap))
        ck("the chain did not loop back to c1", len(chain["c1"]) == 1, str(chain["c1"]))

        print("== daily cap and an agent already running are recorded ==")
        store.con.execute("UPDATE catalog_agents SET daily_cap = 1 WHERE name = 'capt'")
        SCRIPT["capt"] = ("finding", "done", None, None)
        runner.run_now("capt", "watch", "before", "")
        await wait_for(lambda: settled(store, "capt", 1))
        r = await cx.put("/api/agents/builtin/triage", json={
            "trigger": "watch", "prompt": "look",
            "handoffs": [{"verdict": "investigate", "agent": "rca"},
                         {"verdict": "escalate", "agent": "rca2", "cooldown": "2h"},
                         {"verdict": "capme", "agent": "capt"}]})
        assert r.status_code == 200, r.text
        SCRIPT["triage"] = ("finding", "capme", "inventory", "service")
        runner.run_now("triage", "watch", "inventory", "")
        capped = await wait_for(lambda: settled(store, "capt", 2))
        cr = (capped or [{}, {}])[-1]
        ck("over the daily cap: a capped run under the parent, not a silent drop",
           cr.get("status") == "capped" and cr.get("woken_by") == "handoff"
           and cr.get("parent_run_id") == runs(store, "triage")[-1]["id"]
           and "cap of 1 runs" in (cr.get("error") or ""), str(cr))
        runner._inflight.add(("rca2", "orders"))
        SCRIPT["triage"] = ("finding", "escalate", "orders", "service")
        runner.run_now("triage", "watch", "orders", "")
        busy = await wait_for(lambda: settled(store, "rca2", 1))
        runner._inflight.discard(("rca2", "orders"))
        br = (busy or [{}])[0]
        ck("already running for the entity: a capped run saying so",
           br.get("status") == "capped" and "already running for orders" in (br.get("error") or "")
           and br.get("woken_by") == "handoff", str(br))

        print("== timeline ==")
        tl = (await cx.get(f"/api/projects/{a}/timeline?limit=200")).json()
        watch = [t for t in tl["threads"] if t["kind"] == "firing" and t["trigger"] == "watch"]
        ck("the trigger's firing is one thread", len(watch) == 1, str(len(watch)))
        tri_item = watch[0]["runs"][0] if watch and watch[0]["runs"] else {}
        kids = tri_item.get("children") or []
        ck("the handed-off run nests under the triage run",
           [(k["agent"], k["woken_by"]) for k in kids] == [("rca", "handoff")], str(kids)[:400])
        ck("handed-off runs are not threads of their own",
           not any(t["kind"] == "run" and t["runs"][0]["woken_by"] == "handoff"
                   for t in tl["threads"]))
        chain_t = next((t for t in tl["threads"] if t["kind"] == "run" and t["id"] == root), None)
        order = []
        item = chain_t["runs"][0] if chain_t else None
        while item:
            order.append((item["agent"], item["status"]))
            item = item["children"][0] if item["children"] else None
        ck("the chain reads as one thread, c1 to the capped c5",
           order == [("c1", "ok"), ("c2", "ok"), ("c3", "ok"), ("c4", "ok"), ("c5", "capped")],
           str(order))

        print("== export and import ==")
        doc = yaml.safe_load((await cx.get("/api/catalog/export")).text)
        exported = next(x for x in doc["agents"] if x["name"] == "triage")
        ck("the export carries the handoffs", exported.get("handoffs") == [
            {"verdict": "investigate", "agent": "rca", "cooldown": "30m"},
            {"verdict": "escalate", "agent": "rca2", "cooldown": "2h"},
            {"verdict": "capme", "agent": "capt", "cooldown": "30m"}], str(exported))
        ck("an agent without handoffs exports none",
           "handoffs" not in next(x for x in doc["agents"] if x["name"] == "rca"))
        r = await cx.put("/api/agents/builtin/triage", json={"trigger": "watch", "prompt": "look",
                                                             "handoffs": []})
        ck("an empty list clears them",
           r.status_code == 200 and store.get_catalog_agent("triage")["handoffs"] == [], r.text)
        r = await cx.post("/api/catalog/import", json={"yaml": yaml.safe_dump({"agents": [exported]})})
        ck("importing the exported agent brings them back",
           r.status_code == 200
           and store.get_catalog_agent("triage")["handoffs"] == exported["handoffs"], r.text)
        bad_doc = {**exported, "handoffs": [{"verdict": "x", "agent": "ghost"}]}
        r = await cx.post("/api/catalog/import", json={"yaml": yaml.safe_dump({"agents": [bad_doc]})})
        ck("an import naming an unknown agent is refused",
           r.status_code == 400 and "unknown agent" in r.text, r.text)
        ck("and changes nothing",
           store.get_catalog_agent("triage")["handoffs"] == exported["handoffs"])

        print("== deleting the target ==")
        r = await cx.delete("/api/agents/builtin/rca")
        ck("the delete says which agents lost a handoff",
           r.status_code == 200 and r.json().get("handoffs_removed_from") == ["other", "triage"], r.text)
        ck("the handoff to it is gone, the others stay",
           [h["agent"] for h in store.get_catalog_agent("triage")["handoffs"]] == ["rca2", "capt"],
           str(store.get_catalog_agent("triage")["handoffs"]))
        # a stale entry (written straight to the store) is skipped with a note, not an error
        store.upsert_catalog_agent("triage", "watch", "look",
                                   handoffs=[{"verdict": "investigate", "agent": "rca",
                                              "cooldown": "0s"}])
        SCRIPT["triage"] = ("finding", "investigate", "stale", "service")
        runner.run_now("triage", "watch", "stale", "")
        n = len(runs(store, "triage"))
        await wait_for(lambda: settled(store, "triage", n))
        await asyncio.sleep(0.2)
        last = runs(store, "triage")[-1]
        ck("a handoff to an agent that is gone is a note on the run",
           {"kind": "custom", "label": "handoff to rca skipped: no such agent"}
           in (last["results"] or []), str(last["results"]))

        print("== the findings chain still works beside handoffs ==")
        from tares.config import CatalogError, validate_agent_dict
        trigs = {"esc": {"sources": ["findings"], "filters": []}}
        try:
            validate_agent_dict({"name": "x", "trigger": "esc", "prompt": "p"}, {"esc"}, trigs)
            ck("the findings loop guard still refuses an unfiltered findings trigger", False)
        except CatalogError:
            ck("the findings loop guard still refuses an unfiltered findings trigger", True)

    print(f"\n{P} passed, {F} failed")
    sys.exit(1 if F else 0)


asyncio.run(main())
