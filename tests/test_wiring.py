"""Reusable parts, projects hold the wiring (P-TR-216, TR-352..355).

Covers: one agent used by two projects, each waking it on its own trigger: each run belongs to
the project whose wiring started it (its timeline, results and totals only); a project wiring
the same trigger to the same agent as another: the agent runs once and the run shows in both;
handoffs per project (the same verdict hands off to different agents); pausing one project
leaves the other's wiring running; deleting a project keeps the parts another project uses and
deletes the rest; export carries the wiring and import restores it; the one-time upgrade from
an agent's own subscription to its project's wiring.

The model loop is a stand-in that concludes from a script per agent.

Run: .venv/bin/python tests/test_wiring.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TMP = tempfile.mkdtemp()
os.environ["TARES_DB"] = os.path.join(TMP, "t.duckdb")
os.environ["TARES_CATALOG"] = os.path.join(TMP, "none.yaml")
os.environ["TARES_TRIGGER_DEBOUNCE_SECONDS"] = "0"
os.environ["TARES_OTLP_GRPC_PORT"] = "off"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

import httpx
import yaml

from tares.builtin_agents import AgentRunner

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


SCRIPT: dict = {}   # agent -> verdict its next run concludes with


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    verdict = SCRIPT.get(agent["name"], "done")
    summary = f"{agent['name']} looked at {key}"
    concluded.update({"outcome": "finding", "summary": summary, "verdict": verdict,
                      "key": None, "label": None, "produced": []})
    await asyncio.sleep(0.05)
    return summary, 1, 1, [], False, ""


AgentRunner._loop = fake_loop

SOURCE = {"name": "evt", "connector": "webhook", "poll": "5s",
          "config": {"event_type": "log", "text_template": "{msg}",
                     "labels": [{"name": "service", "field": "service", "primary": True},
                                {"name": "team", "field": "team"}]}}


def trigger(name, team, project):
    return {"name": name, "project": project, "sources": ["evt"], "key_field": "service",
            "cooldown": "1h", "filters": [{"field": "team", "op": "eq", "value": team}],
            "condition": {"aggregate": "count", "predicate": "> 0", "window": "5m"}}


async def settle(store, n_before, tries=300):
    """Wait until more runs than `n_before` exist and none is running."""
    for _ in range(tries):
        rs = store.list_agent_runs(limit=1000)
        if len(rs) > n_before and all(r["status"] != "running" for r in rs):
            return rs
        await asyncio.sleep(0.03)
    return store.list_agent_runs(limit=1000)


async def main():
    from tares.daemon import make_app
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        store = app.state.store
        app.state.agents.attach_loop()

        async def mk(kind, body, status=201):
            r = await cx.post(f"/api/{kind}", json=body)
            assert r.status_code == status, (kind, r.status_code, r.text)
            return r.json()

        a = (await mk("projects", {"template": "custom", "name": "Alpha", "objects": []}))["id"]
        b = (await mk("projects", {"template": "custom", "name": "Beta", "objects": []}))["id"]
        await mk("sources", {**SOURCE, "project": a})
        await mk("triggers", trigger("watch_a", "a", a))
        await mk("triggers", trigger("watch_b", "b", b))
        await mk("agents/builtin", {"name": "looker", "trigger": "watch_a", "prompt": "look",
                                    "project": a})
        for n in ("rca_a", "rca_b"):
            await mk("agents/builtin", {"name": n, "trigger": "watch_a", "prompt": "dig",
                                        "project": a if n == "rca_a" else b})
        r = await cx.post("/api/agents/builtin/looker/enable")
        ck("looker on in Alpha (its maker)", r.status_code == 200 and r.json()["project"] == a, r.text)

        print("== one agent, two projects, each its own wake-up ==")
        r = await cx.put("/api/agents/builtin/looker", json={"project": b, "trigger": "watch_b",
                                                             "prompt": "look"})
        ck("Beta wires looker to its own trigger", r.status_code == 200, r.text)
        r = await cx.post("/api/agents/builtin/looker/enable", params={"project": b})
        ck("and turns it on there", r.status_code == 200 and r.json()["project"] == b, r.text)
        eq("each project's wiring, on",
           sorted((w["project"] == a, w["trigger"], w["enabled"])
                  for w in store.list_wakes(agent="looker")),
           [(False, "watch_b", True), (True, "watch_a", True)])
        eq("the agent's own trigger is still Alpha's", store.get_catalog_agent("looker")["trigger"],
           "watch_a")
        ck("looker is in both projects", sorted(store.projects_using("agent", "looker"))
           == sorted([a, b]))

        n0 = len(store.list_agent_runs(limit=1000))
        await cx.post("/ingest/evt", json={"service": "pay", "team": "a", "msg": "x"})
        rs = await settle(store, n0)
        run_a = next(r for r in rs if r["key"] == "pay")
        eq("an Alpha firing: the run belongs to Alpha only", store.run_projects(run_a["id"]), [a])
        n1 = len(rs)
        await cx.post("/ingest/evt", json={"service": "cart", "team": "b", "msg": "x"})
        rs = await settle(store, n1)
        run_b = next(r for r in rs if r["key"] == "cart")
        eq("a Beta firing: the run belongs to Beta only", store.run_projects(run_b["id"]), [b])
        res_a = (await cx.get(f"/api/projects/{a}/results")).json()
        res_b = (await cx.get(f"/api/projects/{b}/results")).json()
        eq("Alpha's results: its run only", [x["entity"] for x in res_a["results"]], ["pay"])
        eq("Beta's results: its run only", [x["entity"] for x in res_b["results"]], ["cart"])
        tl_b = (await cx.get(f"/api/projects/{b}/timeline")).json()["threads"]
        eq("Beta's timeline: its firing only", [t.get("trigger") for t in tl_b], ["watch_b"])

        print("== an agent's runs and stats on a project are that project's ==")
        rows = (await cx.get("/api/agents/builtin", params={"project": b})).json()["agents"]
        lk = next(x for x in rows if x["name"] == "looker")
        eq("Beta sees its own wiring and one run", (lk["trigger"], lk["enabled"], lk["stats"]["runs"]),
           ("watch_b", True, 1))
        runs_b = (await cx.get("/api/agents/builtin/looker/runs", params={"project": b})).json()
        eq("its runs list is Beta's", [r["key"] for r in runs_b], ["cart"])
        r = await cx.post("/api/projects", json={"template": "custom", "name": "Gamma",
                                                 "objects": [{"kind": "agent", "name": "rca_b"}]})
        g = r.json()["id"]
        ck("a project that takes in an agent another made does not copy its handoffs",
           r.status_code == 201 and not store.list_handoffs(project=g)
           and store.list_wakes(project=g, agent="rca_b")[0]["enabled"] is False,
           (store.list_handoffs(project=g), store.list_wakes(project=g)))
        await cx.delete(f"/api/projects/{g}")
        ck("deleting it keeps rca_b (Beta made it)", store.get_catalog_agent("rca_b") is not None)

        print("== the same wiring in two projects: one run, shown in both ==")
        r = await cx.put("/api/triggers/watch_a", json={**trigger("watch_a", "a", b)})
        ck("Beta uses watch_a too", r.status_code == 200
           and sorted(store.projects_using("trigger", "watch_a")) == sorted([a, b]), r.text)
        store.set_wake(b, "watch_a", "looker", enabled=True)
        app.state.runtime.reload_catalog()
        n2 = len(store.list_agent_runs(limit=1000))
        await cx.post("/ingest/evt", json={"service": "ship", "team": "a", "msg": "x"})
        rs = await settle(store, n2)
        ship = [r for r in rs if r["key"] == "ship" and r["agent"] == "looker"]
        eq("looker ran once", len(ship), 1)
        eq("the run belongs to both projects", sorted(store.run_projects(ship[0]["id"])),
           sorted([a, b]))
        d = store.dispatches_where("dispatch_id", [ship[0]["dispatch_id"]])[0]
        eq("so does the firing", sorted(store.dispatch_projects(d["dispatch_id"])), sorted([a, b]))
        store.remove_wakes(project=b, trigger="watch_a")

        print("== handoffs per project ==")
        r = await cx.put("/api/agents/builtin/looker", json={
            "project": a, "trigger": "watch_a", "prompt": "look",
            "handoffs": [{"verdict": "investigate", "agent": "rca_a"}]})
        ck("Alpha: investigate -> rca_a", r.status_code == 200, r.text)
        r = await cx.put("/api/agents/builtin/looker", json={
            "project": b, "trigger": "watch_b", "prompt": "look",
            "handoffs": [{"verdict": "investigate", "agent": "rca_b"}]})
        ck("Beta: investigate -> rca_b", r.status_code == 200, r.text)
        eq("each project holds its own handoff",
           sorted((h["project"] == a, h["agent"]) for h in store.list_handoffs(from_agent="looker")),
           [(False, "rca_b"), (True, "rca_a")])
        SCRIPT["looker"] = "investigate"
        n3 = len(store.list_agent_runs(limit=1000))
        await cx.post("/ingest/evt", json={"service": "auth", "team": "b", "msg": "x"})
        await settle(store, n3 + 1)
        await asyncio.sleep(0.3)
        rs = await settle(store, n3 + 1)
        auth = {r["agent"]: r for r in rs if r["key"] == "auth"}
        ck("a Beta firing hands off to Beta's target", "rca_b" in auth and "rca_a" not in auth,
           sorted(auth))
        eq("the handoff run belongs to Beta", store.run_projects(auth["rca_b"]["id"]), [b])
        SCRIPT["looker"] = "done"

        print("== pausing one project leaves the other running ==")
        r = await cx.post(f"/api/projects/{b}/pause")
        ck("Beta paused", r.status_code == 200 and r.json()["status"] == "paused", r.text[:200])
        ck("looker off in Beta, still on in Alpha",
           not store.agent_enabled("looker", b) and store.agent_enabled("looker", a))
        trigs = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}
        ck("watch_b (Beta's only) paused, watch_a (Alpha's too) not",
           trigs["watch_b"]["paused"] and not trigs["watch_a"]["paused"])
        n4 = len(store.list_agent_runs(limit=1000))
        await cx.post("/ingest/evt", json={"service": "inv", "team": "a", "msg": "x"})
        rs = await settle(store, n4)
        ck("Alpha's firing still runs looker", any(r["key"] == "inv" for r in rs))
        r = await cx.post(f"/api/projects/{b}/resume")
        ck("Beta resumed: looker on there again", r.status_code == 200
           and store.agent_enabled("looker", b), r.text[:200])

        print("== export and import carry the wiring ==")
        y = (await cx.get("/api/catalog/export")).text
        doc = yaml.safe_load(y)
        wires = doc.get("wiring") or []
        ck("the export has a wiring section with both projects' wake-ups",
           {"project": "Beta", "wake": "watch_b", "agent": "looker", "enabled": True} in wires
           and {"project": "Alpha", "wake": "watch_a", "agent": "looker", "enabled": True} in wires,
           json.dumps(wires)[:400])
        ck("and the per-project handoffs",
           any({k: w.get(k) for k in ("project", "agent", "verdict", "to")}
               == {"project": "Beta", "agent": "looker", "verdict": "investigate", "to": "rca_b"}
               for w in wires), json.dumps(wires)[:400])
        store.set_handoffs(b, "looker", [])
        r = await cx.post("/api/catalog/import", json={"yaml": y, "mode": "merge"})
        ck("re-import restores Beta's handoff", r.status_code == 200 and
           [h["agent"] for h in store.list_handoffs(project=b, from_agent="looker")] == ["rca_b"],
           r.text[:200])

        print("== deleting a project keeps what another uses ==")
        r = await cx.delete(f"/api/projects/{a}")
        body = r.json()
        ck("delete -> 200", r.status_code == 200, r.text[:300])
        ck("looker (made by Alpha, used by Beta) is kept, now made by Beta",
           {"kind": "agent", "name": "looker", "project": b} in body["kept_shared"]
           and store.get_catalog_agent("looker")["owned_by"] == b, json.dumps(body)[:400])
        ck("rca_a (Alpha's only) is deleted", "agent:rca_a" in body["deleted"]
           and store.get_catalog_agent("rca_a") is None, json.dumps(body)[:400])
        ck("Alpha's wiring is gone, Beta's stays",
           not store.list_wakes(project=a) and store.list_wakes(project=b, agent="looker"))
        await cx.aclose()

    print("== the upgrade: an agent's subscription becomes its project's wiring ==")
    from tares.store import Store
    path = os.path.join(TMP, "old.duckdb")
    st = Store(path)
    uid = st.default_project_id()
    st.upsert_catalog_trigger("t1", ["s1"], {"aggregate": "count", "predicate": "> 0",
                                            "window": "5m"}, {}, "5m")
    st.upsert_catalog_agent("old_agent", "t1", "look",
                            handoffs=[{"verdict": "investigate", "agent": "other"}])
    st.con.execute("UPDATE catalog_agents SET owned_by = ? WHERE name = 'old_agent'", [uid])
    st.con.execute("DELETE FROM project_wiring")
    st.add_subscription("sub_old", "t1", "tares://agent/old_agent", created_by="tares")
    st.con.execute("DELETE FROM settings WHERE key = 'wiring_moved'")
    st.con.close()
    st = Store(path)
    eq("its trigger and on/off became the project's wake-up",
       st.list_wakes(agent="old_agent"),
       [{"project": uid, "trigger": "t1", "agent": "old_agent", "enabled": True}])
    eq("its handoffs became the project's", [(h["verdict"], h["agent"])
                                             for h in st.list_handoffs(from_agent="old_agent")],
       [("investigate", "other")])
    ck("the old subscription is gone",
       not any("tares://agent/" in s["url"] for s in st.list_all_subscriptions()))

    print("== the upgrade: each project's skills become skills of the cell ==")
    p2 = "uc_other00001"
    st.create_project(p2, "custom", "Other", {"objects": []})
    st.con.execute("DELETE FROM shared_skills")
    st.con.execute("DELETE FROM usecase_objects WHERE kind = 'skill'")
    for proj, name, body in ((uid, "codes", "E42 is a decline"), (p2, "codes", "E42 is a decline"),
                             (uid, "runbook", "restart it"), (p2, "runbook", "page someone")):
        st.con.execute("INSERT INTO skills (project, name, description, body, created_at, "
                       "updated_at) VALUES (?, ?, 'd', ?, now(), now())", [proj, name, body])
    st.con.execute("DELETE FROM settings WHERE key = 'skills_shared'")
    st.con.close()
    st = Store(path)
    eq("the same skill in two projects is one, used by both",
       sorted(st.projects_using("skill", "codes")), sorted([uid, p2]))
    eq("the same name with other text keeps both, the second renamed",
       ({x["name"] for x in st.list_skills(uid)} >= {"runbook"},
        [x["name"] for x in st.list_skills(p2) if x["name"].startswith("runbook")],
        st.get_skill(p2, "runbook-2")["body"]),
       (True, ["runbook-2"], "page someone"))
    st.con.close()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
