"""The goal-first project page: goal, outline, headline and next step, results, handled, health.

Covers the goal (create, update, one line and the length limit, the default project, a
template's GOAL, the once-only upgrade of existing template projects, export), the outline's
sentences (count, an average over a field, a schedule, filters, cooldown, the handoff chain), the
headline and next step a run concludes with (stored, and derived from older notes), the results
feed (one result per chain, no_action, today's totals, paging, an external finding), a result's
steps, the handled mark, and health (working, grouped source errors, a push source that never
received anything, no agent that can run, the daily cap, silence, paused).

The model loop is a stand-in that concludes from a script per agent, so the real run path and
the real handoff step run without a model.

Run: .venv/bin/python tests/test_goal_first.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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

from tares import goal as G
from tares.builtin_agents import AgentRunner, parse_conclude
from tares.config import Condition, SourceCfg, TriggerCfg
from tares.projects.base import PlannedObject, Template
from tares.projects.registry import register

P = F = 0


def ck(label, cond, detail=""):
    global P, F
    P += 1 if cond else 0
    F += 0 if cond else 1
    print(("  ok   " if cond else "  FAIL ") + label + ("" if cond else f"  {detail}"))


def eq(label, got, want):
    ck(label, got == want, f"got {got!r}, want {want!r}")


# agent name -> dict(outcome, verdict, key, label, summary, headline, next_step, skills)
SCRIPT: dict = {}


async def fake_loop(self, agent, trigger_name, key, payload, provider, model, usage, tracer=None,
                    obs=None, concluded=None, produced=None, skills_loaded=None, handoff=False):
    s = SCRIPT.get(agent["name"], {})
    summary = s.get("summary", f"{agent['name']} looked at {key}.")
    concluded.update({"outcome": s.get("outcome", "finding"), "summary": summary,
                      "verdict": s.get("verdict"), "key": s.get("key"), "label": s.get("label"),
                      "headline": s.get("headline"), "next_step": s.get("next_step"),
                      "produced": []})
    for sk in s.get("skills") or []:
        skills_loaded.append(sk)
    return summary, 1, 1, [], False, ""


AgentRunner._loop = fake_loop


class GoalDemo(Template):
    key = "goal_demo"
    title = "Goal demo"
    GOAL = "Show that a template's goal is the default."

    def plan(self, params):
        return [PlannedObject("skill", "skill:demo-notes",
                              {"name": "demo-notes", "description": "notes for the demo",
                               "body": "Nothing to see."})]


register(GoalDemo())


class DescDemo(Template):
    """A template whose planned trigger has a description, for the once-only fill."""
    key = "desc_demo"
    title = "Description demo"

    def plan(self, params):
        return [PlannedObject("source", "source", {**WEBHOOK_SRC, "name": "desc-alerts"}),
                PlannedObject("trigger", "trigger", {
                    "name": "desc-alert", "sources": ["desc-alerts"], "key_field": "service",
                    "description": "an alert fires for a service",
                    "condition": {"aggregate": "count", "predicate": "> 0", "window": "1m"},
                    "cooldown": "5m"})]


WEBHOOK_SRC = {"connector": "webhook", "poll": "5s",
               "config": {"event_type": "log", "text_template": "{msg}",
                          "labels": [{"name": "service", "field": "service", "primary": True}]}}
register(DescDemo())

WEBHOOK = {"connector": "webhook", "poll": "5s",
           "config": {"event_type": "log", "text_template": "{msg}",
                      "labels": [{"name": "service", "field": "service", "primary": True}]}}


async def wait_for(fn, tries=300):
    for _ in range(tries):
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.03)
    return fn()


def settled(store, agent, n):
    rs = list(reversed(store.list_agent_runs(agent, limit=100)))
    return rs if len(rs) >= n and all(r["status"] != "running" for r in rs) else None


def no_em_dash(label, obj):
    text = json.dumps(obj, default=str)
    ck(f"no em dash in {label}", "—" not in text and "\\u2014" not in text)


# ── phrasing, without the daemon ─────────────────────────────────────────────
def phrasing():
    print("== outline phrasing ==")
    src = {"checkout-errors": SourceCfg("checkout-errors", "log", "webhook", 5,
                                        {"labels": [{"name": "service", "field": "service",
                                                     "primary": True}]}),
           "api": SourceCfg("api", "log", "webhook", 5, {})}

    def trig(cond, **kw):
        return TriggerCfg(name=kw.pop("name", "t"), sources=kw.pop("sources", ["checkout-errors"]),
                          condition=cond, **kw)

    count = trig(Condition("count", "> 5", "5m"), cooldown_seconds=300)
    eq("count", G.wake_sentence(count, src),
       "Wakes when checkout-errors gets more than 5 events in 5 minutes for one service.")
    eq("cooldown", G.cooldown_sentence(count, src),
       "Then waits 5 minutes before waking again for the same service.")
    avg = trig(Condition("avg", "> 300", "5m", field="latency_ms"), key_field="service")
    eq("average over a field", G.condition_clause(avg, src),
       "the average latency_ms on checkout-errors goes above 300 in 5 minutes for one service")
    lab = {"labels": [{"name": "service", "field": "service", "primary": True}]}
    sre = {**src, "demo_logs": SourceCfg("demo_logs", "log", "docker_logs", 5, lab),
           "demo_metrics": SourceCfg("demo_metrics", "metric", "prometheus", 5, lab),
           "demo_alerts": SourceCfg("demo_alerts", "log", "prometheus_alerts", 5, lab)}
    total = trig(Condition("sum", "> 0", "1m", field="alert_active"), key_field="service",
                 sources=["demo_logs", "demo_metrics", "demo_alerts"])
    eq("a total across several sources", G.wake_sentence(total, sre),
       "Wakes when the total alert_active across any of its 3 sources goes above 0 in 1 minute "
       "for one service.")
    eq("the highest, two named sources", G.condition_clause(
        trig(Condition("max", ">= 90", "10m", field="cpu"), sources=["checkout-errors", "api"]),
        src), "the highest cpu across checkout-errors and api reaches 90 in 10 minutes for one "
              "service")
    eq("the lowest, one source with a filter", G.condition_clause(
        trig(Condition("min", "< 5", "1h", field="free_gb"), sources=["api"],
             filters=[{"field": "env", "op": "eq", "value": "prod"}]), src),
       "the lowest free_gb of events matching env = prod on api drops below 5 in 1 hour")
    eq("a total that is exactly a number", G.condition_clause(
        trig(Condition("sum", "== 0", "1h", field="orders"), sources=["api"]), src),
       "the total orders on api is 0 over 1 hour")
    fsrc = {**src, "findings": SourceCfg("findings", "log", "finding", 5, lab)}
    after = [{"field": "agent", "op": "eq", "value": "watcher"}]
    eq("another agent's findings, in plain words", G.wake_sentence(trig(
        Condition("count", "> 0", "10m"), sources=["findings"], key_field="service",
        filters=after + [{"field": "verdict", "op": "eq", "value": "investigate"}]), fsrc),
       "Wakes when watcher flags a service for a closer look.")
    eq("another agent's findings, any verdict", G.condition_clause(trig(
        Condition("count", "> 0", "10m"), sources=["findings"], filters=after), fsrc),
       "watcher reports a finding on a service")
    eq("a pasted repo URL is titled owner/name", G.source_title(SourceCfg(
        "ctx_x", "log", "github", 5, {"repo": "https://github.com/acme/shop.git"})), "acme/shop")
    rows = [{"title": "demo-prometheus.example", "description": "Prometheus"},
            {"title": "demo-prometheus.example", "description": "Prometheus alerts"},
            {"title": "glassflow/tares", "description": "GitHub commits"}]
    G.tell_apart(rows, "title", "description")
    eq("two sources on one host are told apart by kind, a unique one is left alone",
       [r["title"] for r in rows],
       ["Prometheus, demo-prometheus.example", "Prometheus alerts, demo-prometheus.example",
        "glassflow/tares"])
    ag = [{"name": n, "runs_on": "trigger", "handoffs": []} for n in ("rca", "watcher")]
    eq("an agent woken by another's findings comes after it",
       [a["name"] for a in G._chain_order(ag, {"rca": "watcher"})], ["watcher", "rca"])
    for label, x in (("sum", total), ("avg", avg)):
        ck(f"no 'events' after the sources ({label})",
           " events " not in G.condition_clause(x, sre) + " ", G.condition_clause(x, sre))

    print("== a trigger's own description ==")
    desc = trig(Condition("sum", "> 0", "1m", field="alert_active"), key_field="service",
                sources=["demo_logs", "demo_metrics", "demo_alerts"], cooldown_seconds=300,
                description="Prometheus fires an alert for the demo service")
    eq("the wake sentence says it", G.wake_sentence(desc, sre),
       "Wakes when Prometheus fires an alert for the demo service.")
    eq("the cooldown sentence stays", G.cooldown_sentence(desc, sre),
       "Then waits 5 minutes before waking again for the same service.")
    eq("the lead says it", G._lead(desc, sre),
       "When Prometheus fires an alert for the demo service")
    parts = {"triggers": [desc], "external": False, "targets": {},
             "agents": [{"name": "incident-first-look", "trigger": "t", "enabled": True,
                         "handoffs": []}]}
    eq("the project sentence says it", G.outline_sentence(parts, sre),
       "When Prometheus fires an alert for the demo service, incident-first-look looks first.")
    sched_d = trig(Condition("count", "> 0", "1h", every=3600.0),
                   description="the hourly check comes round")
    eq("a schedule with a description", (G.wake_sentence(sched_d, src), G._lead(sched_d, src)),
       ("Wakes when the hourly check comes round.", "When the hourly check comes round"))
    sched = trig(Condition("count", "> 0", "10m", every=600.0), cooldown_seconds=300)
    eq("schedule", G.wake_sentence(sched, src), "Wakes every 10 minutes.")
    eq("a schedule has no cooldown sentence", G.cooldown_sentence(sched, src), None)
    filt = trig(Condition("count", ">= 3", "1h"), sources=["api"],
                filters=[{"field": "level", "op": "eq", "value": "error"},
                         {"field": "status", "op": "gt", "value": "499"},
                         {"field": "msg", "op": "contains", "value": "timeout"},
                         {"field": "env", "op": "neq", "value": "dev"}])
    eq("filters, no entity label", G.wake_sentence(filt, src),
       "Wakes when api gets at least 3 events matching level = error and status above 499 and "
       "msg containing timeout and env is not dev in 1 hour.")
    eq("no label: the cooldown names none", G.cooldown_sentence(filt, src),
       "Then waits 5 minutes before waking again.")
    sched_f = trig(Condition("count", "> 0", "15m", every=900.0),
                   filters=[{"field": "level", "op": "eq", "value": "error"}])
    eq("schedule with filters", G.wake_sentence(sched_f, src),
       "Wakes every 15 minutes, for events matching level = error.")
    one = trig(Condition("count", "> 0", "5m"), sources=["checkout-errors", "api"])
    eq("any event, two sources", G.condition_clause(one, src),
       "an event arrives on checkout-errors and api for one service")
    grouped = trig(Condition("count", "> 1", "2m", group_by=["env", "app"]))
    eq("grouped by labels", G.condition_clause(grouped, src),
       "checkout-errors gets more than 1 event in 2 minutes for one env and app")
    eq("durations", [G.duration_words(x) for x in (30, 60, 5400, 7200, 86400)],
       ["30 seconds", "1 minute", "90 minutes", "2 hours", "1 day"])

    print("== the description column, on a database from before it ==")
    import duckdb
    from tares.store import Store
    old = os.path.join(TMP, "old.duckdb")
    con = duckdb.connect(old)
    con.execute("CREATE TABLE catalog_triggers (name TEXT PRIMARY KEY, sources JSON, "
                "filters JSON, key_field TEXT, condition JSON, emit JSON, cooldown TEXT, "
                "created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ, paused BOOLEAN DEFAULT FALSE)")
    con.execute("INSERT INTO catalog_triggers (name, sources, filters, key_field, condition, "
                "emit, cooldown) VALUES ('old', '[\"api\"]', '[]', '', "
                "'{\"aggregate\": \"count\", \"predicate\": \"> 0\", \"window\": \"5m\"}', "
                "'{}', '5m')")
    con.close()
    st = Store(old)
    eq("an older trigger reads with no description",
       [(t["name"], t["description"]) for t in st.list_catalog_triggers()], [("old", "")])
    st.upsert_catalog_trigger("old", ["api"], {"aggregate": "count", "predicate": "> 0",
                                               "window": "5m"}, {}, "5m",
                              description="an event arrives")
    st.con.close()
    st = Store(old)   # the migration runs again on a database that has the column
    eq("the migration runs twice and keeps it",
       [(t["name"], t["description"]) for t in st.list_catalog_triggers()],
       [("old", "an event arrives")])
    st.con.close()

    print("== headline and next step, derived from older notes ==")
    note_h = ("## Payment provider outage\n\n8 checkout payments failed in one burst. The gateway "
              "timed out.\n\n**Next step:** escalate to the payment provider.\n\nMore detail.")
    eq("heading", G.derive_headline(note_h), "Payment provider outage")
    eq("next step after a bold label", G.derive_next_step(note_h),
       "Escalate to the payment provider.")
    eq("summary skips the heading", G.summary_of(note_h),
       "8 checkout payments failed in one burst. The gateway timed out.")
    note_b = "**Bad client input, checkout kept working.** A burst of 422s from one client."
    eq("bold lead line", G.derive_headline(note_b), "Bad client input, checkout kept working.")
    note_l = "**Conclusion:** the disk is full on db-1. It filled overnight."
    eq("a bold label: the sentence after it", G.derive_headline(note_l),
       "the disk is full on db-1.")
    note_s = ("Checkout is failing for `payments-api` since 14:02. It started after the deploy.\n"
              "1) what: 500s\n2) cause: a bad config\n3) Suggested next action: roll back the "
              "deploy and watch the error rate")
    eq("first sentence, markdown stripped", G.derive_headline(note_s),
       "Checkout is failing for payments-api since 14:02.")
    eq("numbered next action", G.derive_next_step(note_s),
       "Roll back the deploy and watch the error rate")
    eq("Recommendation: label", G.derive_next_step("All fine.\nRecommendation: nothing yet."),
       "Nothing yet.")
    eq("Next: label after a sentence", G.derive_next_step("It is noise. Next: ignore it."),
       "Ignore it.")
    eq("a heading named Next steps", G.derive_next_step("# Note\n\n## Next steps\n\n- page "
                                                        "the on-call\n- open a ticket\n\n## Other"),
       "Page the on-call open a ticket")
    eq("no label: no next step", G.derive_next_step("Everything is normal."), None)
    long = "word " * 60
    h = G.derive_headline(long)
    ck("a long headline is cut to 100 characters", h is not None and len(h) <= 100, repr(h))
    eq("empty note", (G.derive_headline(""), G.derive_next_step(None)), (None, None))
    out, err = parse_conclude({"outcome": "finding", "summary": "x", "headline": " A\nB ",
                               "next_step": "n" * 400})
    ck("conclude takes headline and next_step, one line and cut",
       err is None and out["headline"] == "A B" and len(out["next_step"]) == 300, str(out))

    print("== the narration an agent opens with is not the headline ==")
    note_f = ("I have a complete picture. Here's the full analysis:\n\n## Argus 5xx, rius-prod\n\n"
              "Three 500s in the window before, none now.")
    eq("filler sentences, then a heading", G.derive_headline(note_f), "Argus 5xx, rius-prod")
    eq("the summary skips them too", G.summary_of(note_f, "Argus 5xx, rius-prod"),
       "Three 500s in the window before, none now.")
    note_g = ("I have sufficient evidence from the timeline to diagnose this without additional "
              "reads.\n\nA burst of 404 probes hit the docs site; nothing is broken.")
    eq("a filler paragraph is skipped", G.derive_headline(note_g),
       "A burst of 404 probes hit the docs site; nothing is broken.")
    note_k = "Let me summarize. The disk on db-1 is full. It filled overnight."
    eq("filler inside the first paragraph", G.derive_headline(note_k), "The disk on db-1 is full.")
    eq("a finding that ends with a colon is kept",
       G.derive_headline("Database connection pool exhausted:"), "Database connection pool exhausted:")
    eq("a note that is all narration keeps it", G.derive_headline("Let me check that."),
       "Let me check that.")
    eq("a note that is not narration is kept", G.derive_headline("I have seen this before: "
                                                                 "the cache node restarts."),
       "I have seen this before: the cache node restarts.")

    print("== source errors in plain words ==")
    eq("GitHub token", G._plain_error("github", "HTTPStatusError: Client error '401 "
                                                "Unauthorized' for url 'https://api.github.com'"),
       ("can't reach GitHub", "the token was rejected", "Update the token"))
    eq("refused connection", G._plain_error("loki", "ConnectError: [Errno 61] Connection "
                                                    "refused"),
       ("can't collect events", "the address cannot be reached", "Check the address"))


async def main():
    phrasing()
    from tares.daemon import make_app
    from tares.projects import Engine
    app = make_app()
    async with app.router.lifespan_context(app):
        cx = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
        store = app.state.store
        runtime = app.state.runtime
        runner = app.state.agents
        runner.attach_loop()

        async def mk(kind, body, status=201):
            r = await cx.post(f"/api/{kind}", json=body)
            assert r.status_code == status, (kind, r.status_code, r.text)
            return r.json()

        print("== goal ==")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "Checkout",
                                                 "objects": [],
                                                 "goal": "  Catch checkout outages early\n and "
                                                         "find the root cause. "})
        ck("create with a goal", r.status_code == 201, r.text)
        a = r.json()["id"]
        eq("the goal is one line, trimmed", r.json().get("goal"),
           "Catch checkout outages early and find the root cause.")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "Long",
                                                 "objects": [], "goal": "x" * 201})
        ck("a goal over 200 characters is refused", r.status_code == 400 and "200" in r.text,
           r.text)
        ck("and nothing was created", store.get_project_by_name("Long") is None)
        listed = {p["id"]: p for p in (await cx.get("/api/projects")).json()["projects"]}
        eq("the list carries it", listed[a].get("goal"),
           "Catch checkout outages early and find the root cause.")
        default = store.default_project_id()
        ck("the default project has no goal yet", listed[default].get("goal", "x") is None)
        summ = (await cx.get(f"/api/projects/{a}/summary")).json()
        eq("the summary carries it", summ.get("goal"),
           "Catch checkout outages early and find the root cause.")
        r = await cx.put(f"/api/projects/{a}", json={"goal": "Catch checkout outages early."})
        ck("PUT with only a goal", r.status_code == 200
           and r.json()["goal"] == "Catch checkout outages early.", r.text)
        r = await cx.put(f"/api/projects/{a}", json={"goal": "y" * 201})
        ck("PUT refuses a long goal", r.status_code == 400, r.text)
        r = await cx.put(f"/api/projects/{default}", json={"goal": "Everything else"})
        ck("the default project takes a goal", r.status_code == 200
           and (await cx.get(f"/api/projects/{default}")).json()["goal"] == "Everything else",
           r.text)
        r = await cx.put(f"/api/projects/{default}", json={"params": {}})
        ck("the default project still refuses other edits", r.status_code == 400, r.text)
        r = await cx.put(f"/api/projects/{default}", json={"goal": ""})
        ck("an empty goal clears it", r.status_code == 200
           and (await cx.get(f"/api/projects/{default}")).json()["goal"] is None, r.text)
        r = await cx.post("/api/projects", json={"template": "goal_demo", "name": "Demo one"})
        eq("a template project gets the template's GOAL", r.json().get("goal"),
           "Show that a template's goal is the default.")
        demo = r.json()["id"]
        r = await cx.post("/api/projects", json={"template": "goal_demo", "name": "Demo two",
                                                 "goal": "My own words."})
        eq("an explicit goal wins over the template's", r.json().get("goal"), "My own words.")
        from tares.projects.registry import get_template
        for key in ("ai_sre_demo", "rius_rca", "challenger_workflow", "shared_code_context"):
            g = get_template(key).GOAL
            ck(f"{key} has a plain goal", bool(g) and len(g) <= 200 and "—" not in g, g)

        print("== goal upgrade ==")
        store.update_project(demo, goal=None)
        store.set_setting("template_goals_filled", None)
        n = Engine(store).fill_template_goals()
        ck("an existing template project gets its template's goal",
           n == 1 and store.get_project(demo)["goal"] == "Show that a template's goal is the "
                                                         "default.", f"{n}")
        ck("custom and default projects stay without one",
           store.get_project(default)["goal"] is None
           and store.get_project_by_name("Demo two")["goal"] == "My own words.")
        store.update_project(demo, goal=None)
        ck("it runs once: a goal cleared later stays cleared",
           Engine(store).fill_template_goals() == 0 and store.get_project(demo)["goal"] is None)
        await cx.put(f"/api/projects/{demo}", json={"goal": "Demo goal"})
        doc = yaml.safe_load((await cx.get("/api/catalog/export")).text)
        exp = {p["name"]: p for p in doc.get("projects") or []}
        ck("the export carries goals", exp.get("Checkout", {}).get("goal")
           == "Catch checkout outages early." and exp.get("Demo one", {}).get("goal")
           == "Demo goal", str(exp)[:300])
        r = await cx.post("/api/catalog/import", json={"yaml": yaml.safe_dump({"projects": [
            {"template": "goal_demo", "name": "Demo one", "goal": "Imported goal", "params": {}},
            {"template": "default", "name": "Default", "goal": "Imported default goal"}]})})
        ck("an import sets the goal, the default project's too", r.status_code == 200
           and store.get_project(demo)["goal"] == "Imported goal"
           and store.get_project(default)["goal"] == "Imported default goal", r.text)

        print("== outline ==")
        await mk("sources", {**WEBHOOK, "name": "checkout-errors", "project": a})
        await mk("triggers", {"name": "spike", "project": a, "sources": ["checkout-errors"],
                              "key_field": "service", "cooldown": "5m",
                              "condition": {"aggregate": "count", "predicate": "> 5",
                                            "window": "5m"}})
        await mk("agents/builtin", {"name": "checkout-rca", "trigger": "spike", "prompt": "dig",
                                    "project": a})
        await mk("agents/builtin", {"name": "checkout-triage", "trigger": "spike",
                                    "prompt": "look", "project": a,
                                    "handoffs": [{"verdict": "investigate",
                                                  "agent": "checkout-rca"}]})
        await cx.post("/api/agents/builtin/checkout-triage/enable")
        await mk(f"projects/{a}/skills", {"name": "checkout-error-codes",
                                          "description": "Which codes are an outage.",
                                          "body": "503 with payment gateway is an outage."})
        ol = (await cx.get(f"/api/projects/{a}/outline")).json()
        eq("the sentence", ol["sentence"],
           "When checkout-errors gets more than 5 events in 5 minutes for one service, "
           "checkout-triage looks first. If it concludes investigate, checkout-rca digs in.")
        eq("watches", [(w["source"], w["connector"], w["description"], w["state"], w["detail"])
                       for w in ol["watches"]],
           [("checkout-errors", "webhook", "Inbound webhook", "waiting",
             "Waiting for its first event")])
        eq("wakes", ol["wakes"], [{
            "trigger": "spike",
            "sentence": "Wakes when checkout-errors gets more than 5 events in 5 minutes for "
                        "one service.",
            "cooldown_sentence": "Then waits 5 minutes before waking again for the same "
                                 "service.",
            "paused": False}])
        agents = {x["name"]: x for x in ol["agents"]}
        eq("the first agent", (agents["checkout-triage"]["sentence"],
                               agents["checkout-triage"]["runs_on"],
                               agents["checkout-triage"]["enabled"]),
           ("checkout-triage looks first.", "trigger", True))
        eq("its handoffs", agents["checkout-triage"]["handoffs"],
           [{"verdict": "investigate", "agent": "checkout-rca", "cooldown": "30m"}])
        eq("the handed-off agent", (agents["checkout-rca"]["sentence"],
                                    agents["checkout-rca"]["runs_on"]),
           ("checkout-rca digs in when checkout-triage concludes investigate, at most once per "
            "service every 30 minutes.", "handoff"))
        eq("skills", [(s["name"], s["description"], s["loaded_by"]) for s in ol["skills"]],
           [("checkout-error-codes", "Which codes are an outage.", [])])
        no_em_dash("the outline", ol)

        print("== a trigger's description, through the API ==")
        r = await cx.post("/api/projects", json={"template": "custom", "name": "Described",
                                                 "objects": []})
        dp = r.json()["id"]
        await mk("sources", {**WEBHOOK, "name": "pager", "project": dp})
        base = {"project": dp, "sources": ["pager"], "key_field": "service", "cooldown": "5m",
                "condition": {"aggregate": "sum", "field": "alert_active", "predicate": "> 0",
                              "window": "1m"}}
        for bad, why in (("a\nb", "one line"), ("x" * 161, "160"),
                         ("an alert — fires", "em dash")):
            r = await cx.post("/api/triggers", json={**base, "name": "bad", "description": bad})
            ck(f"a description refused: {why}", r.status_code == 400 and why in r.text, r.text)
        r = await cx.post("/api/triggers", json={**base, "name": "paged",
                                                 "description": " an alert fires in the demo "
                                                                "service. "})
        ck("create with a description", r.status_code == 201, r.text)
        rows = {t["name"]: t for t in (await cx.get("/api/triggers")).json()}
        eq("the list carries it, trimmed", rows["paged"]["description"],
           "an alert fires in the demo service")
        await mk("agents/builtin", {"name": "pager-look", "trigger": "paged", "prompt": "look",
                                    "project": dp})
        await cx.post("/api/agents/builtin/pager-look/enable")
        ol = (await cx.get(f"/api/projects/{dp}/outline")).json()
        eq("the project sentence uses it", ol["sentence"],
           "When an alert fires in the demo service, pager-look looks first.")
        eq("the wake sentence uses it", (ol["wakes"][0]["sentence"],
                                         ol["wakes"][0]["cooldown_sentence"]),
           ("Wakes when an alert fires in the demo service.",
            "Then waits 5 minutes before waking again for the same service."))
        r = await cx.put("/api/triggers/paged", json={**base, "name": "paged",
                                                      "cooldown": "10m"})
        ck("an update without it keeps it", r.status_code == 200 and {
            t["name"]: t for t in (await cx.get("/api/triggers")).json()}["paged"][
            "description"] == "an alert fires in the demo service", r.text)
        doc = (await cx.get("/api/catalog/export")).text
        exported = {t["name"]: t for t in yaml.safe_load(doc)["triggers"]}
        eq("the export carries it", exported["paged"].get("description"),
           "an alert fires in the demo service")
        ck("a trigger without one exports without the key", "description" not in exported["spike"])
        r = await cx.put("/api/triggers/paged", json={**base, "name": "paged", "description": ""})
        ck("an empty description clears it", r.status_code == 200 and {
            t["name"]: t for t in (await cx.get("/api/triggers")).json()}["paged"][
            "description"] == "", r.text)
        ol = (await cx.get(f"/api/projects/{dp}/outline")).json()
        eq("then the sentence is said from the rule", ol["wakes"][0]["sentence"],
           "Wakes when the total alert_active on pager goes above 0 in 1 minute for one "
           "service.")
        r = await cx.post("/api/catalog/import", json={"yaml": doc})
        ck("an import brings it back", r.status_code == 200 and {
            t["name"]: t for t in (await cx.get("/api/triggers")).json()}["paged"][
            "description"] == "an alert fires in the demo service", r.text)
        bad_doc = yaml.safe_load(doc)
        bad_doc["triggers"] = [{**exported["paged"], "description": "a\nb"}]
        r = await cx.post("/api/catalog/import", json={"yaml": yaml.safe_dump(bad_doc)})
        ck("an import refuses a bad description", r.status_code == 400 and "one line" in r.text,
           r.text)

        print("== template trigger descriptions, filled once ==")
        r = await cx.post("/api/projects", json={"template": "desc_demo", "name": "Desc one"})
        ck("a template project's trigger gets the template's description",
           r.status_code == 201 and {t["name"]: t for t in store.list_catalog_triggers()}[
               "desc-alert"]["description"] == "an alert fires for a service", r.text)
        store.set_trigger_description("desc-alert", None)
        store.set_setting("template_trigger_descriptions_filled", None)
        eng = Engine(store)
        n = eng.fill_template_trigger_descriptions()
        ck("an older template project's trigger is filled",
           n == 1 and {t["name"]: t for t in store.list_catalog_triggers()}["desc-alert"][
               "description"] == "an alert fires for a service", f"{n}")
        store.set_trigger_description("desc-alert", None)
        ck("it runs once: a description cleared later stays cleared",
           eng.fill_template_trigger_descriptions() == 0
           and {t["name"]: t for t in store.list_catalog_triggers()}["desc-alert"][
               "description"] == "")
        from tares.projects.registry import get_template
        sre = [o for o in get_template("ai_sre_demo").plan(
            get_template("ai_sre_demo").validate({})) if o.kind == "trigger"]
        eq("the AI SRE demo's trigger says what happens", sre[0].spec.get("description"),
           "Prometheus fires an alert for the demo service")

        print("== health before anything arrived ==")
        h = (await cx.get(f"/api/projects/{a}/health")).json()
        eq("setting up", (h["state"], h["message"]),
           ("setting_up", "Waiting for the first event from checkout-errors."))
        ck("the push source waits for its first event", any(
            i["message"] == "checkout-errors is waiting for its first event."
            and i["severity"] == "warning" and i["view"] == "source:checkout-errors"
            for i in h["issues"]), str(h["issues"]))

        print("== a firing, a triage and a handoff ==")
        SCRIPT["checkout-triage"] = {"verdict": "investigate", "key": "payments-api",
                                     "label": "service", "skills": ["checkout-error-codes"],
                                     "summary": "A clean gateway failure on payments-api.",
                                     "headline": "Gateway failures on payments-api"}
        SCRIPT["checkout-rca"] = {"verdict": "rca", "key": "payments-api", "label": "service",
                                  "summary": "8 checkout payments failed in one burst.\n\nThe "
                                             "provider's gateway is timing out.",
                                  "headline": "Payment provider outage",
                                  "next_step": "Escalate to the payment provider."}
        for i in range(6):
            await cx.post("/ingest/checkout-errors", json={"service": "payments-api",
                                                          "msg": f"503 {i}"})
        rca = await wait_for(lambda: settled(store, "checkout-rca", 1))
        tri = settled(store, "checkout-triage", 1)
        ck("triage handed off and rca ran", bool(rca) and bool(tri)
           and rca[0]["status"] == "ok" and rca[0]["parent_run_id"] == tri[0]["id"],
           str((tri, rca))[:400])
        ck("the headline and next step are stored on the run",
           rca[0]["headline"] == "Payment provider outage"
           and rca[0]["next_step"] == "Escalate to the payment provider.", str(rca[0]))
        ev = [json.loads(p) for (p,) in store.con.execute(
            "SELECT payload FROM events WHERE source = 'findings'").fetchall()]
        ev = [e for e in ev if e.get("run_id") == rca[0]["id"]]
        pl = ev[0] if ev else {}
        ck("the finding event carries them", pl.get("headline") == "Payment provider outage"
           and pl.get("next_step") == "Escalate to the payment provider.", str(ev)[:300])
        store.con.execute("UPDATE agent_runs SET duration_ms = 8000, cost_usd = 0.02 "
                          "WHERE id = ?", [tri[0]["id"]])
        store.con.execute("UPDATE agent_runs SET duration_ms = 16000, cost_usd = 0.03 "
                          "WHERE id = ?", [rca[0]["id"]])

        print("== more results ==")
        # a no_op with a note: a no_action result
        SCRIPT["checkout-triage"] = {"outcome": "no_op", "summary": "Bad client input, checkout "
                                                                     "kept working."}
        runner.run_now("checkout-triage", "spike", "orders-api", "")
        await wait_for(lambda: settled(store, "checkout-triage", 2))
        # a no_op without a note: not a result
        SCRIPT["checkout-triage"] = {"outcome": "no_op", "summary": ""}
        runner.run_now("checkout-triage", "spike", "quiet-api", "")
        await wait_for(lambda: settled(store, "checkout-triage", 3))
        # a finding with a verdict that needs nothing, and no handoff: no_action by its verdict
        SCRIPT["checkout-triage"] = {"verdict": "fine", "summary": "Normal traffic.",
                                     "headline": "Normal traffic",
                                     "next_step": "Keep an eye on it."}
        runner.run_now("checkout-triage", "spike", "cart-api", "")
        await wait_for(lambda: settled(store, "checkout-triage", 4))
        # an older run with neither headline nor next step: derived from the note
        store.start_agent_run("run_old1", "checkout-triage", "spike", "", "db-1", "h",
                              woken_by="manual", project=a)
        store.finish_agent_run("run_old1", "ok", finding="## Disk full on db-1\n\nThe disk "
                               "filled overnight.\n\nNext step: add space to db-1.",
                               outcome="finding", verdict="investigate")
        # an external agent's finding
        r = await cx.post(f"/api/projects/{a}/findings", json={
            "entity": "search-api", "finding": "Search latency doubled after the index rebuild.",
            "verdict": "slow", "agent": "outside-bot"})
        ck("an external finding is recorded", r.status_code == 201, r.text)
        ext_id = r.json()["run_id"]

        res = (await cx.get(f"/api/projects/{a}/results")).json()
        by_entity = {x["entity"]: x for x in res["results"]}
        eq("one result per chain, newest first",
           [x["entity"] for x in res["results"]],
           ["search-api", "db-1", "cart-api", "orders-api", "payments-api"])
        top = by_entity["payments-api"]
        eq("the triage that handed off is not the result, its child is",
           (top["id"], top["chain"], top["kind"], top["verdict"]),
           (rca[0]["id"], ["checkout-triage", "checkout-rca"], "action", "rca"))
        eq("headline, summary and next step", (top["headline"], top["summary"], top["next_step"]),
           ("Payment provider outage", "8 checkout payments failed in one burst.",
            "Escalate to the payment provider."))
        eq("cost and time of the whole chain", (top["cost_usd"], top["duration_ms"]),
           (0.05, 24000))
        ck("its thread is the firing", top["thread"] and top["handled"] is None
           and top["external"] is False, str(top))
        eq("a no_op with a note is a no_action result",
           (by_entity["orders-api"]["kind"], by_entity["orders-api"]["headline"]),
           ("no_action", "Bad client input, checkout kept working."))
        ck("a no_op without a note is not a result", "quiet-api" not in by_entity)
        eq("a verdict that needs nothing is no_action", by_entity["cart-api"]["kind"],
           "no_action")
        old = by_entity["db-1"]
        eq("derived for an older note", (old["headline"], old["next_step"], old["kind"]),
           ("Disk full on db-1", "Add space to db-1.", "action"))
        ext = by_entity["search-api"]
        eq("an external finding is a result", (ext["id"], ext["external"], ext["chain"],
                                               ext["kind"]),
           (ext_id, True, ["outside-bot"], "no_action"))
        eq("today", res["today"], {"looked_at": 6, "found": 2, "spent_usd": 0.05})
        eq("no more pages", res["next_before"], None)
        no_em_dash("the results", res)

        p1 = (await cx.get(f"/api/projects/{a}/results?limit=2")).json()
        ck("a page of two with a cursor", len(p1["results"]) == 2 and p1["next_before"],
           str(p1)[:300])
        p2 = (await cx.get(f"/api/projects/{a}/results", params={
            "limit": 2, "before": p1["next_before"]})).json()
        p3 = (await cx.get(f"/api/projects/{a}/results", params={
            "limit": 2, "before": p2["next_before"]})).json()
        eq("paging with before walks all results once",
           [x["id"] for x in p1["results"] + p2["results"] + p3["results"]],
           [x["id"] for x in res["results"]])
        eq("the last page has no cursor", p3["next_before"], None)
        r = await cx.get(f"/api/projects/{a}/results?before=yesterday")
        ck("a bad cursor is a 400", r.status_code == 400, r.text)

        print("== a result's steps ==")
        det = (await cx.get(f"/api/projects/{a}/results/{rca[0]['id']}")).json()
        eq("the note in full", det.get("note"), SCRIPT["checkout-rca"]["summary"])
        eq("the steps", [s["text"] for s in det.get("steps") or []], [
            "6 events from payments-api, more than the 5 in 5 minutes that wakes the project",
            "checkout-triage looked (8 s, used checkout-error-codes) and concluded investigate",
            "checkout-rca dug in (16 s) and wrote this finding"])
        ck("each step has a time", all(s["at"] for s in det["steps"]))
        eq("the detail is the result too", (det["id"], det["chain"], det["headline"]),
           (rca[0]["id"], ["checkout-triage", "checkout-rca"], "Payment provider outage"))
        det = (await cx.get(f"/api/projects/{a}/results/{by_entity['orders-api']['id']}")).json()
        ck("a no_op step", det["steps"][-1]["text"].startswith("checkout-triage looked (")
           and det["steps"][-1]["text"].endswith("and found nothing that needs action"),
           str(det["steps"]))
        det = (await cx.get(f"/api/projects/{a}/results/{ext_id}")).json()
        eq("an external step", [s["text"] for s in det["steps"]],
           ["outside-bot, an outside agent, recorded this finding"])
        r = await cx.get(f"/api/projects/{a}/results/{tri[0]['id']}")
        ck("a triage that handed off still has its own result page", r.status_code == 200
           and r.json()["id"] == tri[0]["id"], r.text[:200])
        r = await cx.get(f"/api/projects/{a}/results/run_nope")
        ck("an unknown run is a 404", r.status_code == 404, r.text)
        no_em_dash("a result", det)

        print("== handled ==")
        r = await cx.post(f"/api/projects/{a}/results/{rca[0]['id']}/handled",
                          json={"handled": True})
        ck("mark handled", r.status_code == 200 and r.json()["handled"]["by"] == "console"
           and r.json()["handled"]["at"], r.text)
        top = next(x for x in (await cx.get(f"/api/projects/{a}/results")).json()["results"]
                   if x["id"] == rca[0]["id"])
        ck("the result says it", top["handled"] and top["handled"]["by"] == "console", str(top))
        r = await cx.post(f"/api/projects/{a}/results/{rca[0]['id']}/handled",
                          json={"handled": False})
        ck("clear it", r.status_code == 200 and r.json()["handled"] is None
           and store.get_agent_run(rca[0]["id"])["handled_at"] is None, r.text)
        r = await cx.post(f"/api/projects/{a}/results/{rca[0]['id']}/handled",
                          json={"handled": "yes"})
        ck("handled must be a boolean", r.status_code == 400, r.text)
        r = await cx.post(f"/api/projects/{demo}/results/{rca[0]['id']}/handled",
                          json={"handled": True})
        ck("another project's run is a 404", r.status_code == 404, r.text)

        r = await cx.post(f"/api/projects/{a}/findings", json={
            "entity": "images-api", "finding": "The CDN certificate expired at 09:00.",
            "headline": "CDN certificate expired", "next_step": "Renew the certificate.",
            "agent": "outside-bot"})
        ck("an outside agent can send a headline and next step", r.status_code == 201, r.text)
        mine = next(x for x in (await cx.get(f"/api/projects/{a}/results")).json()["results"]
                    if x["entity"] == "images-api")
        eq("they show on its result", (mine["headline"], mine["next_step"], mine["kind"]),
           ("CDN certificate expired", "Renew the certificate.", "action"))
        r = await cx.post(f"/api/projects/{a}/findings", json={
            "entity": "x", "finding": "y", "headline": "h" * 101, "agent": "outside-bot"})
        ck("a headline over 100 characters is refused", r.status_code == 400, r.text)
        print("== health ==")
        h = (await cx.get(f"/api/projects/{a}/health")).json()
        eq("working", (h["state"], h["message"], h["issues"]),
           ("working", "Receiving from 1 source, last event just now. 2 agents ready.", []))
        catalog = runtime.catalog
        project = store.get_project(a)
        later = datetime.now(timezone.utc) + timedelta(hours=3)
        h = G.project_health(store, catalog, a, project, runtime.health_snapshot(), now=later)
        eq("a source that speaks in bursts is not silent when quiet (quiet errors are good news)",
           (h["state"], h["issues"]), ("working", []))
        # a steady source: events in many hours of the day
        real_hours = store.recent_ingest_hours
        store.recent_ingest_hours = lambda name, since: list(range(8))
        h = G.project_health(store, catalog, a, project, runtime.health_snapshot(), now=later)
        eq("a steady source silent for much longer than usual", (h["state"], h["issues"][:1]), ("attention", [{
            "severity": "warning", "message": "Nothing has arrived on checkout-errors for 3 hours.",
            "fix": "Check the connection", "view": "source:checkout-errors"}]))
        ol = G.outline(store, catalog, a, runtime.health_snapshot(), now=later)
        eq("the outline says silent too", (ol["watches"][0]["state"], ol["watches"][0]["detail"]),
           ("silent", "Nothing for 3 hours"))
        store.recent_ingest_hours = real_hours
        store.con.execute("UPDATE catalog_agents SET daily_cap = 2 WHERE name = 'checkout-triage'")
        h = (await cx.get(f"/api/projects/{a}/health")).json()
        eq("the daily cap", (h["state"], h["issues"]), ("attention", [{
            "severity": "warning",
            "message": "checkout-triage reached its limit of 2 runs a day, so it does not run "
                       "until its earlier runs are a day old.",
            "fix": "Raise the limit", "view": "agent:checkout-triage"}]))
        store.con.execute("UPDATE catalog_agents SET daily_cap = NULL "
                          "WHERE name = 'checkout-triage'")
        await cx.post("/api/agents/builtin/checkout-triage/disable")
        h = (await cx.get(f"/api/projects/{a}/health")).json()
        eq("no agent can run", (h["state"], h["issues"][:1]), ("attention", [{
            "severity": "error",
            "message": "No agent is turned on, so nothing looks at what arrives.",
            "fix": "Turn on an agent", "view": "agent:checkout-rca"}]))
        await cx.post("/api/agents/builtin/checkout-triage/enable")
        r = await cx.post(f"/api/projects/{a}/pause")
        h = (await cx.get(f"/api/projects/{a}/health")).json()
        eq("paused", (h["state"], h["message"]),
           ("paused", "Triggers are off and agents do not run. Sources keep collecting."))
        await cx.post(f"/api/projects/{a}/resume")

        # source errors: five GitHub sources with the same rejected token are one issue
        g = (await cx.post("/api/projects", json={"template": "custom", "name": "Repos",
                                                  "objects": []})).json()["id"]
        fake_sources, fake_health = {}, {}
        for i in range(5):
            name = f"repo-{i}"
            store.upsert_catalog_source(name, "commit", "github", "2m", {"repo": f"o/r{i}"})
            store.put_in_project("source", name, g)
            fake_sources[name] = SourceCfg(name, "commit", "github", 120, {"repo": f"o/r{i}"})
            fake_health[name] = {"status": "error", "last_ingest": None,
                                 "last_error": "HTTPStatusError: Client error '401 Unauthorized' "
                                               "for url 'https://api.github.com/repos/o/r'"}
        fake_sources["logs"] = SourceCfg("logs", "log", "loki", 5, {})
        store.upsert_catalog_source("logs", "log", "loki", "5s", {})
        store.put_in_project("source", "logs", g)
        fake_health["logs"] = {"status": "error", "last_ingest": None,
                               "last_error": "ConnectError: [Errno 61] Connection refused"}
        fake = SimpleNamespace(sources=fake_sources, triggers=[])
        h = G.project_health(store, fake, g, store.get_project(g), fake_health)
        eq("errors grouped, most important first", [(i["severity"], i["message"], i["fix"],
                                                     i["view"]) for i in h["issues"]][:3], [
            ("error", "5 sources can't reach GitHub: the token was rejected.", "Update the token",
             "settings:github"),
            ("error", "logs can't collect events: the address cannot be reached.",
             "Check the address", "source:logs"),
            ("error", "No agent is set up, so nothing looks at what arrives.", "Add an agent",
             "how")])
        eq("nothing arrived, but something is broken: that leads", h["state"], "attention")
        no_em_dash("health", h)

        print("== an agent outside Tares that checks in with its project key ==")
        k = (await cx.post(f"/api/projects/{g}/keys", json={"name": "claude-code"})).json()
        h = G.project_health(store, fake, g, store.get_project(g), fake_health)
        msgs = [i["message"] for i in h["issues"]]
        ck("a project key counts as an agent: no 'No agent is set up'",
           not any("No agent is set up" in m for m in msgs), str(msgs))
        ck("until it uses its key, it is not connected yet",
           "Your agent claude-code has not connected with its key yet." in msgs, str(msgs))
        store.touch_api_key(k["id"])
        h = G.project_health(store, fake, g, store.get_project(g), fake_health)
        ck("once it used its key, no issue about it",
           not any("claude-code" in i["message"] for i in h["issues"]), str(h["issues"]))
        ol = G.outline(store, fake, g, fake_health)
        eq("the Setup page lists it", ol["outside"], [{
            "name": "claude-code", "sentence": "claude-code, your own agent, checks the project "
                                               "for what happened.", "joined": True}])
        trig = next(t for t in catalog.triggers if t.project == a)
        parts = {"triggers": [trig], "agents": [], "targets": {}, "external": True,
                 "outside": G.outside_agents(store, g)}
        ck("the header says it sees what happened when it checks in",
           G.outline_sentence(parts, catalog.sources).endswith(
               ", claude-code sees it when it checks in."),
           G.outline_sentence(parts, catalog.sources))
        await cx.delete(f"/api/keys/{k['id']}")
        eq("a revoked key is no agent", G.outside_agents(store, g), [])

        print("== looked at today: only firings an agent was on ==")
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        before = store.project_threads_since(a, midnight)
        store.log_dispatch("d-nobody", trig.name, "svc", "count", 0, 0, "", project=a)
        eq("a firing no agent was on is not counted", store.project_threads_since(a, midnight),
           before)
        store.log_dispatch("d-someone", trig.name, "svc", "count", 1, 1, "", project=a)
        eq("a firing an agent was on is", store.project_threads_since(a, midnight), before + 1)
        await cx.aclose()


asyncio.run(main())
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
